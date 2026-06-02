import sys
import os
import subprocess
import urllib.request
import urllib.error
import json
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QLabel, QMessageBox, QProgressBar,
    QMenu, QFileDialog, QDialog, QHeaderView, QScrollArea,
    QCheckBox, QFrame
)
from PySide6.QtCore import Qt, QObject, QThread, Signal, QPoint, QSettings

from packaging import version
from packaging.version import InvalidVersion

if sys.platform == "win32":
    SUBPROCESS_PLATFORM_KWARGS = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    SUBPROCESS_PLATFORM_KWARGS = {}


def get_venv_python(project_path: str) -> str | None:
    project = Path(project_path)

    windows_python_root = project / 'Scripts' / 'python.exe'
    if windows_python_root.exists():
        return str(windows_python_root)

    unix_python_root = project / 'bin' / 'python'
    if unix_python_root.exists():
        return str(unix_python_root)

    venv_names = ['venv', '.venv', 'env', '.env']

    for venv_name in venv_names:
        venv_path = project / venv_name
        if venv_path.exists() and venv_path.is_dir():
            windows_python = venv_path / 'Scripts' / 'python.exe'
            if windows_python.exists():
                return str(windows_python)

            unix_python = venv_path / 'bin' / 'python'
            if unix_python.exists():
                return str(unix_python)

    return None


def get_venv_name(project_path: str) -> str | None:
    project = Path(project_path)

    if (project / 'Scripts' / 'python.exe').exists() or \
       (project / 'bin' / 'python').exists():
        return "(root)"

    venv_names = ['venv', '.venv', 'env', '.env']

    for venv_name in venv_names:
        venv_path = project / venv_name
        if venv_path.exists() and venv_path.is_dir():
            if (venv_path / 'Scripts' / 'python.exe').exists() or \
               (venv_path / 'bin' / 'python').exists():
                return venv_name
    return None


def safe_version_key(ver_string):
    try:
        return (0, version.parse(ver_string))
    except InvalidVersion:
        return (1, ver_string)


class InstalledPackagesWorker(QObject):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, python_executable):
        super().__init__()
        self.python_executable = python_executable

    def run(self):
        try:
            result = subprocess.run(
                [self.python_executable, "-m", "pip", "list", "--format=json"],
                capture_output=True,
                text=True,
                timeout=60,
                **SUBPROCESS_PLATFORM_KWARGS
            )
            if result.returncode == 0:
                try:
                    packages = json.loads(result.stdout)
                except json.JSONDecodeError:
                    self.error.emit("Could not parse package list (unexpected non-JSON output from pip).")
                    return
                package_list = [(pkg['name'], pkg['version']) for pkg in packages]
                package_list.sort(key=lambda x: x[0].lower())
                self.finished.emit(package_list)
            else:
                self.error.emit(result.stderr or "Failed to get package list")
        except subprocess.TimeoutExpired:
            self.error.emit("Command timed out")
        except Exception as e:
            self.error.emit(str(e))


class PackageDependenciesWorker(QObject):
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, python_executable, package_names):
        super().__init__()
        self.python_executable = python_executable
        self.package_names = package_names

    def run(self):
        try:
            dependencies = {}
            batch_size = 50
            for i in range(0, len(self.package_names), batch_size):
                batch = self.package_names[i:i + batch_size]
                result = subprocess.run(
                    [self.python_executable, "-m", "pip", "show"] + batch,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    **SUBPROCESS_PLATFORM_KWARGS
                )

                if result.returncode == 0:
                    current_package = None
                    for line in result.stdout.split('\n'):
                        if line.startswith('Name: '):
                            current_package = line[6:].strip()
                            dependencies[current_package] = {'requires': [], 'required_by': []}
                        elif line.startswith('Requires: ') and current_package:
                            requires = line[10:].strip()
                            if requires:
                                dependencies[current_package]['requires'] = [r.strip() for r in requires.split(',')]
                        elif line.startswith('Required-by: ') and current_package:
                            required_by = line[13:].strip()
                            if required_by:
                                dependencies[current_package]['required_by'] = [r.strip() for r in required_by.split(',')]

            self.finished.emit(dependencies)
        except subprocess.TimeoutExpired:
            self.error.emit("Command timed out")
        except Exception as e:
            self.error.emit(str(e))


class OutdatedPackagesWorker(QObject):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, python_executable):
        super().__init__()
        self.python_executable = python_executable

    def run(self):
        try:
            result = subprocess.run(
                [self.python_executable, "-m", "pip", "list", "--outdated", "--format=json"],
                capture_output=True,
                text=True,
                timeout=300,
                **SUBPROCESS_PLATFORM_KWARGS
            )
            if result.returncode == 0:
                try:
                    packages = json.loads(result.stdout)
                except json.JSONDecodeError:
                    self.error.emit("Could not parse outdated list (unexpected non-JSON output from pip).")
                    return
                outdated_list = [
                    (pkg['name'], pkg['version'], pkg['latest_version'])
                    for pkg in packages
                ]
                self.finished.emit(outdated_list)
            else:
                self.error.emit(result.stderr or "Failed to check outdated packages")
        except subprocess.TimeoutExpired:
            self.error.emit("Command timed out (this operation can be slow)")
        except Exception as e:
            self.error.emit(str(e))


class VersionsWorker(QObject):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, package_name):
        super().__init__()
        self.package_name = package_name

    def run(self):
        try:
            versions = self.get_all_versions(self.package_name)
            self.finished.emit(versions)
        except Exception as e:
            self.error.emit(str(e))

    def get_all_versions(self, package_name):
        url = f"https://pypi.org/pypi/{package_name}/json"
        versions = []
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status != 200:
                    raise Exception(f"PyPI returned status code {response.status}")
                data = json.load(response)
                for ver, release_info in data['releases'].items():
                    if not release_info:
                        continue
                    # Skip versions whose files have all been yanked (withdrawn from PyPI)
                    if all(f.get('yanked', False) for f in release_info):
                        continue
                    release_date = release_info[0].get('upload_time', 'N/A')
                    versions.append((ver, release_date))
                versions = sorted(versions, key=lambda v: safe_version_key(v[0]))
        except urllib.error.URLError as e:
            raise Exception(f"Network error: {str(e)}")
        except TimeoutError:
            raise Exception("Connection timed out")
        except json.JSONDecodeError:
            raise Exception("Invalid response from PyPI")
        except Exception as e:
            raise Exception(f"Error fetching versions: {str(e)}")
        return versions


class PipWorker(QObject):
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, python_executable, package_name, selected_version):
        super().__init__()
        self.python_executable = python_executable
        self.package_name = package_name
        self.selected_version = selected_version

    def run(self):
        try:
            command = [self.python_executable, "-m", "pip", "install",
                      f"{self.package_name}=={self.selected_version}", "--no-deps"]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **SUBPROCESS_PLATFORM_KWARGS
            )
            stdout, stderr = process.communicate(timeout=300)
            if process.returncode == 0:
                self.finished.emit(stdout)
            else:
                self.error.emit(stderr)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            self.error.emit("Installation timed out")
        except Exception as e:
            self.error.emit(str(e))


class PipCheckWorker(QObject):
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, python_executable):
        super().__init__()
        self.python_executable = python_executable

    def run(self):
        try:
            command = [self.python_executable, "-m", "pip", "check"]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **SUBPROCESS_PLATFORM_KWARGS
            )
            stdout, stderr = process.communicate()

            output = ""
            if stdout.strip():
                output = stdout
            else:
                output = "No broken requirements found."
            if stderr:
                output += f"\n\nErrors:\n{stderr}"

            self.finished.emit(output)
        except Exception as e:
            self.error.emit(str(e))


class CommandWorker(QObject):
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, python_executable, command):
        super().__init__()
        self.python_executable = python_executable
        self.command = command

    def run(self):
        try:
            command_parts = self.command.strip().split()
            if not command_parts:
                self.error.emit("No command provided")
                return

            if command_parts[0].lower() == "pip":
                command_parts = [self.python_executable, "-m"] + command_parts

            process = subprocess.Popen(
                command_parts,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **SUBPROCESS_PLATFORM_KWARGS
            )
            stdout, stderr = process.communicate(timeout=120)

            if process.returncode == 0:
                output = stdout if stdout else "Command executed successfully"
                self.finished.emit(output)
            else:
                error_msg = stderr if stderr else f"Command failed with return code {process.returncode}"
                self.error.emit(error_msg)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            self.error.emit("Command timed out")
        except Exception as e:
            self.error.emit(str(e))


class PackageInfoWorker(QObject):
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, package_name):
        super().__init__()
        self.package_name = package_name

    def run(self):
        try:
            url = f"https://pypi.org/pypi/{self.package_name}/json"
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status != 200:
                    raise Exception(f"PyPI returned status code {response.status}")
                data = json.load(response)
                self.finished.emit(data['info'])
        except urllib.error.URLError as e:
            self.error.emit(f"Network error: {str(e)}")
        except TimeoutError:
            self.error.emit("Connection timed out")
        except json.JSONDecodeError:
            self.error.emit("Invalid response from PyPI")
        except Exception as e:
            self.error.emit(f"Error fetching package info: {str(e)}")


class CompareDependenciesWorker(QObject):
    finished = Signal(str, str, list, list)
    error = Signal(str)

    def __init__(self, python_executable, package_name):
        super().__init__()
        self.python_executable = python_executable
        self.package_name = package_name

    def run(self):
        try:
            result = subprocess.run(
                [self.python_executable, "-m", "pip", "show", self.package_name],
                capture_output=True,
                text=True,
                timeout=30,
                **SUBPROCESS_PLATFORM_KWARGS
            )

            current_version = "Unknown"
            current_deps = []

            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if line.startswith('Version: '):
                        current_version = line[9:].strip()
                    elif line.startswith('Requires: '):
                        requires = line[10:].strip()
                        if requires:
                            current_deps = sorted([r.strip() for r in requires.split(',')])

            url = f"https://pypi.org/pypi/{self.package_name}/json"
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status != 200:
                    raise Exception(f"PyPI returned status code {response.status}")
                data = json.load(response)
                latest_version = data['info']['version']
                latest_requires = data['info'].get('requires_dist', []) or []
                latest_deps = sorted([req for req in latest_requires if req])

            self.finished.emit(current_version, latest_version, current_deps, latest_deps)

        except Exception as e:
            self.error.emit(str(e))


class CompareDependenciesDialog(QDialog):
    def __init__(self, parent, package_name, current_version, latest_version, current_deps, latest_deps):
        super().__init__(parent)
        self.setWindowTitle(f"Dependency Comparison - {package_name}")
        self.setMinimumWidth(600)
        self.setFixedHeight(600)
        self.package_name = package_name
        self.current_version = current_version
        self.latest_version = latest_version
        self.current_deps_full = current_deps
        self.latest_deps_full = latest_deps
        self.hide_extras = True

        self.main_layout = QVBoxLayout(self)

        version_layout = QVBoxLayout()
        version_layout.addWidget(QLabel(f"<b>Current Version:</b> {self.current_version}"))
        version_layout.addWidget(QLabel(f"<b>Latest Version:</b> {self.latest_version}"))
        version_layout.addWidget(QLabel("<b>Dependencies:</b>"))
        self.main_layout.addLayout(version_layout)

        self.hide_extras_checkbox = QCheckBox("Hide Extra Dependencies")
        self.hide_extras_checkbox.stateChanged.connect(self.update_display)
        self.main_layout.addWidget(self.hide_extras_checkbox)

        self.deps_layout = QHBoxLayout()
        self.main_layout.addLayout(self.deps_layout)

        self.current_scroll_area = QScrollArea()
        self.current_scroll_area.setWidgetResizable(True)
        self.current_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.current_widget = QWidget()
        self.current_layout = QVBoxLayout(self.current_widget)
        self.current_label = QLabel()
        self.current_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.current_label.setStyleSheet("background-color: #2E2E2E; padding: 10px; color: white;")
        self.current_label.setWordWrap(True)
        self.current_layout.addWidget(self.current_label)
        self.current_layout.addStretch()
        self.current_scroll_area.setWidget(self.current_widget)
        self.current_scroll_area.setMinimumHeight(200)
        self.deps_layout.addWidget(self.current_scroll_area)

        self.latest_scroll_area = QScrollArea()
        self.latest_scroll_area.setWidgetResizable(True)
        self.latest_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.latest_widget = QWidget()
        self.latest_layout = QVBoxLayout(self.latest_widget)
        self.latest_label = QLabel()
        self.latest_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.latest_label.setStyleSheet("background-color: #2E2E2E; padding: 10px; color: white;")
        self.latest_label.setWordWrap(True)
        self.latest_layout.addWidget(self.latest_label)
        self.latest_layout.addStretch()
        self.latest_scroll_area.setWidget(self.latest_widget)
        self.latest_scroll_area.setMinimumHeight(200)
        self.deps_layout.addWidget(self.latest_scroll_area)

        self.changes_scroll = QScrollArea()
        self.changes_scroll.setWidgetResizable(True)
        self.changes_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.changes_widget = QWidget()
        self.changes_layout = QVBoxLayout(self.changes_widget)

        self.changes_title = QLabel("<b>Changes:</b>")
        self.changes_title.setStyleSheet("font-weight: bold;")
        self.changes_layout.addWidget(self.changes_title)

        self.added_label = QLabel()
        self.added_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.added_label.setWordWrap(True)
        self.added_label.setStyleSheet("padding: 5px;")
        self.removed_label = QLabel()
        self.removed_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.removed_label.setWordWrap(True)
        self.removed_label.setStyleSheet("padding: 5px;")

        self.changes_layout.addWidget(self.added_label)
        self.changes_layout.addWidget(self.removed_label)

        self.changes_layout.addStretch()
        self.changes_scroll.setWidget(self.changes_widget)
        self.main_layout.addWidget(self.changes_scroll)

        self.update_display()

    def update_display(self):
        self.hide_extras = self.hide_extras_checkbox.isChecked()
        filtered_current_deps = self.filter_extras(self.current_deps_full)
        filtered_latest_deps = self.filter_extras(self.latest_deps_full)

        if filtered_current_deps:
            current_text = "\n".join(filtered_current_deps)
        else:
            current_text = "No dependencies found."
        self.current_label.setText(f"<b>Current:</b>\n{current_text}")

        if filtered_latest_deps:
            latest_text = "\n".join(filtered_latest_deps)
        else:
            latest_text = "No dependencies found."
        self.latest_label.setText(f"<b>Latest:</b>\n{latest_text}")

        added = set(filtered_latest_deps) - set(filtered_current_deps)
        removed = set(filtered_current_deps) - set(filtered_latest_deps)

        if added:
            added_text = "<b>Added:</b> " + ", ".join(sorted(added))
            self.added_label.setText(added_text)
            self.added_label.setVisible(True)
        else:
            self.added_label.setText("")
            self.added_label.setVisible(False)

        if removed:
            removed_text = "<b>Removed:</b> " + ", ".join(sorted(removed))
            self.removed_label.setText(removed_text)
            self.removed_label.setVisible(True)
        else:
            self.removed_label.setText("")
            self.removed_label.setVisible(False)

        if not added and not removed:
            self.changes_title.setText("<b>Changes:</b>")
            self.added_label.setText("No changes in dependencies.")
            self.removed_label.setText("")
            self.added_label.setVisible(True)
        else:
            self.changes_title.setText("<b>Changes:</b>")

    def filter_extras(self, deps):
        if not self.hide_extras:
            return deps
        filtered = [dep for dep in deps if 'extra' not in dep.lower()]
        return filtered


class OutputDialog(QDialog):
    def __init__(self, parent, title, content):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(600, 400)

        layout = QVBoxLayout(self)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)

        label = QLabel(content)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setWordWrap(True)
        label.setStyleSheet("background-color: #1E1E1E; padding: 10px; color: #CCCCCC; font-family: Consolas, monospace;")

        content_layout.addWidget(label)
        content_layout.addStretch()

        scroll_area.setWidget(content_widget)
        layout.addWidget(scroll_area)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)


class PackageChecker(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python Package Checker")
        self.setMinimumSize(900, 700)

        self.settings = QSettings("BlairChintella", "PythonPackageChecker")

        self.python_executable = None
        self.project_path = None
        self.current_mode = None
        self.outdated_packages = []
        self.installed_packages = []
        self.dependencies_map = {}

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        project_frame = QFrame()
        project_frame.setFrameStyle(QFrame.StyledPanel)
        project_layout = QVBoxLayout(project_frame)

        project_select_layout = QHBoxLayout()
        self.project_path_label = QLabel("No project selected")
        self.project_path_label.setStyleSheet("color: #888888;")
        project_select_layout.addWidget(self.project_path_label, 1)

        self.select_project_button = QPushButton("Select Project")
        self.select_project_button.clicked.connect(self.select_project)
        project_select_layout.addWidget(self.select_project_button)

        project_layout.addLayout(project_select_layout)

        self.venv_status_label = QLabel("")
        project_layout.addWidget(self.venv_status_label)

        layout.addWidget(project_frame)

        button_layout = QHBoxLayout()
        self.check_all_button = QPushButton("Check All")
        self.check_all_button.setEnabled(False)
        button_layout.addWidget(self.check_all_button)
        self.check_outdated_button = QPushButton("Check Outdated")
        self.check_outdated_button.setEnabled(False)
        button_layout.addWidget(self.check_outdated_button)
        self.pip_check_button = QPushButton("Pip Check")
        self.pip_check_button.setEnabled(False)
        button_layout.addWidget(self.pip_check_button)
        layout.addLayout(button_layout)

        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText("Type pip command here to run (e.g., pip install requests)")
        self.command_input.returnPressed.connect(self.execute_command)
        self.command_input.setEnabled(False)
        layout.addWidget(self.command_input)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.results_table = QTableWidget()
        self.results_table.setColumnCount(3)
        self.results_table.setHorizontalHeaderLabels(["Package", "Current Version", "Latest Version"])
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.results_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.results_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results_table.customContextMenuRequested.connect(self.open_context_menu)
        layout.addWidget(self.results_table)

        self.check_all_button.clicked.connect(self.check_all_packages)
        self.check_outdated_button.clicked.connect(self.check_outdated_packages)
        self.pip_check_button.clicked.connect(self.run_pip_check)

        self.restore_geometry()

    def restore_geometry(self):
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

        window_state = self.settings.value("windowState")
        if window_state is not None:
            self.restoreState(window_state)

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        super().closeEvent(event)

    def select_project(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Project Folder",
            "",
            QFileDialog.ShowDirsOnly
        )

        if not folder:
            return

        python_path = get_venv_python(folder)

        if python_path:
            self.project_path = folder
            self.python_executable = python_path
            venv_name = get_venv_name(folder)

            self.project_path_label.setText(folder)
            self.project_path_label.setStyleSheet("color: #FFFFFF;")
            self.venv_status_label.setText(f"Virtual environment detected: {venv_name}")
            self.venv_status_label.setStyleSheet("color: #00AA00;")

            self.check_all_button.setEnabled(True)
            self.check_outdated_button.setEnabled(True)
            self.pip_check_button.setEnabled(True)
            self.command_input.setEnabled(True)

            self.results_table.clearContents()
            self.results_table.setRowCount(0)
            self.current_mode = None
            self.outdated_packages = []
            self.installed_packages = []
            self.dependencies_map = {}

            self.setWindowTitle(f"Python Package Checker - {os.path.basename(folder)}")
        else:
            self.show_message("No Virtual Environment Found",
                            f"No virtual environment was found in:\n{folder}\n\n"
                            "Looked for: venv, .venv, env, .env")
            self.venv_status_label.setText("No virtual environment detected")
            self.venv_status_label.setStyleSheet("color: #AA0000;")

    def check_all_packages(self):
        if not self.python_executable:
            return

        self.current_mode = 'all'
        self.deps_error_message = None
        self.results_table.clearContents()
        self.results_table.setRowCount(0)
        self.results_table.setSortingEnabled(False)
        self.progress_bar.setVisible(True)
        self.check_all_button.setEnabled(False)
        self.check_outdated_button.setEnabled(False)

        self.installed_thread = QThread()
        self.installed_worker = InstalledPackagesWorker(self.python_executable)
        self.installed_worker.moveToThread(self.installed_thread)
        self.installed_thread.started.connect(self.installed_worker.run)
        self.installed_worker.finished.connect(self.on_installed_packages_checked)
        self.installed_worker.error.connect(self.on_worker_error)
        self.installed_worker.finished.connect(self.installed_thread.quit)
        self.installed_worker.finished.connect(self.installed_worker.deleteLater)
        self.installed_worker.error.connect(self.installed_thread.quit)
        self.installed_worker.error.connect(self.installed_worker.deleteLater)
        self.installed_thread.finished.connect(self.installed_thread.deleteLater)
        self.installed_thread.start()

    def on_installed_packages_checked(self, packages):
        self.installed_packages = packages
        self.results_table.setRowCount(len(packages))

        for row, (name, ver) in enumerate(packages):
            self.results_table.setItem(row, 0, QTableWidgetItem(name))
            self.results_table.setItem(row, 1, QTableWidgetItem(ver))
            self.results_table.setItem(row, 2, QTableWidgetItem("N/A"))

        package_names = [pkg[0] for pkg in packages]
        self.deps_thread = QThread()
        self.deps_worker = PackageDependenciesWorker(self.python_executable, package_names)
        self.deps_worker.moveToThread(self.deps_thread)
        self.deps_thread.started.connect(self.deps_worker.run)
        self.deps_worker.finished.connect(self.on_dependencies_fetched)
        self.deps_worker.error.connect(self.on_dependencies_error)
        self.deps_worker.finished.connect(self.deps_thread.quit)
        self.deps_worker.finished.connect(self.deps_worker.deleteLater)
        self.deps_worker.error.connect(self.deps_thread.quit)
        self.deps_worker.error.connect(self.deps_worker.deleteLater)
        self.deps_thread.finished.connect(self.deps_thread.deleteLater)
        self.deps_thread.start()

    def on_dependencies_fetched(self, dependencies):
        self.dependencies_map = dependencies

        for row in range(self.results_table.rowCount()):
            item = self.results_table.item(row, 0)
            if item:
                self.set_tooltip_for_package(row, item.text())

        self.fetch_latest_for_all()

    def on_dependencies_error(self, error_message):
        self.deps_error_message = error_message
        self.fetch_latest_for_all()

    def fetch_latest_for_all(self):
        self.all_outdated_thread = QThread()
        self.all_outdated_worker = OutdatedPackagesWorker(self.python_executable)
        self.all_outdated_worker.moveToThread(self.all_outdated_thread)
        self.all_outdated_thread.started.connect(self.all_outdated_worker.run)
        self.all_outdated_worker.finished.connect(self.on_all_latest_fetched)
        self.all_outdated_worker.error.connect(self.on_all_latest_error)
        self.all_outdated_worker.finished.connect(self.all_outdated_thread.quit)
        self.all_outdated_worker.finished.connect(self.all_outdated_worker.deleteLater)
        self.all_outdated_worker.error.connect(self.all_outdated_thread.quit)
        self.all_outdated_worker.error.connect(self.all_outdated_worker.deleteLater)
        self.all_outdated_thread.finished.connect(self.all_outdated_thread.deleteLater)
        self.all_outdated_thread.start()

    def on_all_latest_fetched(self, outdated_packages):
        outdated_map = {name: latest for name, current, latest in outdated_packages}

        for row in range(self.results_table.rowCount()):
            name_item = self.results_table.item(row, 0)
            current_item = self.results_table.item(row, 1)
            if not name_item or not current_item:
                continue
            latest = outdated_map.get(name_item.text(), current_item.text())
            self.results_table.setItem(row, 2, QTableWidgetItem(latest))

        self.finalize_check_all(updates_available=len(outdated_packages))

    def on_all_latest_error(self, error_message):
        self.finalize_check_all(latest_error=error_message)

    def finalize_check_all(self, updates_available=None, latest_error=None):
        self.progress_bar.setVisible(False)
        self.check_all_button.setEnabled(True)
        self.check_outdated_button.setEnabled(True)
        self.results_table.setSortingEnabled(True)
        self.results_table.sortItems(0, Qt.AscendingOrder)
        self.results_table.scrollToTop()

        message = f"Total packages installed: {len(self.installed_packages)}"
        if updates_available is not None:
            message += f"\nUpdates available: {updates_available}"

        notes = []
        if self.deps_error_message:
            notes.append(f"Could not fetch dependency info: {self.deps_error_message}")
        if latest_error:
            notes.append(f"Could not fetch latest versions: {latest_error}")
        if notes:
            message += "\n\n" + "\n".join(f"Note: {n}" for n in notes)

        self.show_message("Check All Complete", message)

    def check_outdated_packages(self):
        if not self.python_executable:
            return

        self.current_mode = 'outdated'
        self.results_table.clearContents()
        self.results_table.setRowCount(0)
        self.results_table.setSortingEnabled(False)
        self.progress_bar.setVisible(True)
        self.check_all_button.setEnabled(False)
        self.check_outdated_button.setEnabled(False)

        self.outdated_thread = QThread()
        self.outdated_worker = OutdatedPackagesWorker(self.python_executable)
        self.outdated_worker.moveToThread(self.outdated_thread)
        self.outdated_thread.started.connect(self.outdated_worker.run)
        self.outdated_worker.finished.connect(self.on_outdated_packages_checked)
        self.outdated_worker.error.connect(self.on_worker_error)
        self.outdated_worker.finished.connect(self.outdated_thread.quit)
        self.outdated_worker.finished.connect(self.outdated_worker.deleteLater)
        self.outdated_worker.error.connect(self.outdated_thread.quit)
        self.outdated_worker.error.connect(self.outdated_worker.deleteLater)
        self.outdated_thread.finished.connect(self.outdated_thread.deleteLater)
        self.outdated_thread.start()

    def on_outdated_packages_checked(self, outdated_packages):
        self.progress_bar.setVisible(False)
        self.check_all_button.setEnabled(True)
        self.check_outdated_button.setEnabled(True)

        if not outdated_packages:
            self.show_message("Up to Date", "All packages are up to date!")
            return

        self.outdated_packages = outdated_packages
        self.results_table.setRowCount(len(outdated_packages))

        for row, (name, current, latest) in enumerate(outdated_packages):
            self.results_table.setItem(row, 0, QTableWidgetItem(name))
            self.results_table.setItem(row, 1, QTableWidgetItem(current))
            self.results_table.setItem(row, 2, QTableWidgetItem(latest))

        package_names = [pkg[0] for pkg in outdated_packages]
        self.deps_thread = QThread()
        self.deps_worker = PackageDependenciesWorker(self.python_executable, package_names)
        self.deps_worker.moveToThread(self.deps_thread)
        self.deps_thread.started.connect(self.deps_worker.run)
        self.deps_worker.finished.connect(self.on_outdated_dependencies_fetched)
        self.deps_worker.error.connect(self.on_outdated_dependencies_error)
        self.deps_worker.finished.connect(self.deps_thread.quit)
        self.deps_worker.finished.connect(self.deps_worker.deleteLater)
        self.deps_worker.error.connect(self.deps_thread.quit)
        self.deps_worker.error.connect(self.deps_worker.deleteLater)
        self.deps_thread.finished.connect(self.deps_thread.deleteLater)
        self.deps_thread.start()

    def on_outdated_dependencies_fetched(self, dependencies):
        self.dependencies_map = dependencies

        for row in range(self.results_table.rowCount()):
            item = self.results_table.item(row, 0)
            if item:
                self.set_tooltip_for_package(row, item.text())

        self.results_table.setSortingEnabled(True)
        self.results_table.sortItems(0, Qt.AscendingOrder)
        self.results_table.scrollToTop()

        self.show_message("Outdated Packages", f"Total outdated packages: {len(self.outdated_packages)}")

    def on_outdated_dependencies_error(self, error_message):
        self.results_table.setSortingEnabled(True)
        self.results_table.sortItems(0, Qt.AscendingOrder)
        self.results_table.scrollToTop()

        self.show_message("Outdated Packages",
                         f"Total outdated packages: {len(self.outdated_packages)}\n\n"
                         f"Note: Could not fetch dependency info: {error_message}")

    def on_worker_error(self, error_message):
        self.progress_bar.setVisible(False)
        self.check_all_button.setEnabled(True)
        self.check_outdated_button.setEnabled(True)
        self.show_message("Error", f"Error: {error_message}")

    def run_pip_check(self):
        if not self.python_executable:
            return

        self.progress_bar.setVisible(True)
        self.pip_check_button.setEnabled(False)

        self.pip_check_thread = QThread()
        self.pip_check_worker = PipCheckWorker(self.python_executable)
        self.pip_check_worker.moveToThread(self.pip_check_thread)
        self.pip_check_thread.started.connect(self.pip_check_worker.run)
        self.pip_check_worker.finished.connect(self.on_pip_check_finished)
        self.pip_check_worker.error.connect(self.on_pip_check_error)
        self.pip_check_worker.finished.connect(self.pip_check_thread.quit)
        self.pip_check_worker.finished.connect(self.pip_check_worker.deleteLater)
        self.pip_check_worker.error.connect(self.pip_check_thread.quit)
        self.pip_check_worker.error.connect(self.pip_check_worker.deleteLater)
        self.pip_check_thread.finished.connect(self.pip_check_thread.deleteLater)
        self.pip_check_thread.start()

    def on_pip_check_finished(self, output):
        self.progress_bar.setVisible(False)
        self.pip_check_button.setEnabled(True)

        dialog = OutputDialog(self, "Pip Check Results", output)
        dialog.exec()

    def on_pip_check_error(self, error_message):
        self.progress_bar.setVisible(False)
        self.pip_check_button.setEnabled(True)
        self.show_message("Error", f"Pip check failed: {error_message}")

    def execute_command(self):
        if not self.python_executable:
            return

        command = self.command_input.text().strip()
        if not command:
            return

        self.command_input.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.command_input.clear()

        self.command_thread = QThread()
        self.command_worker = CommandWorker(self.python_executable, command)
        self.command_worker.moveToThread(self.command_thread)

        self.command_thread.started.connect(self.command_worker.run)
        self.command_worker.finished.connect(self.on_command_finished)
        self.command_worker.error.connect(self.on_command_error)
        self.command_worker.finished.connect(self.command_thread.quit)
        self.command_worker.error.connect(self.command_thread.quit)
        self.command_worker.finished.connect(self.command_worker.deleteLater)
        self.command_worker.error.connect(self.command_worker.deleteLater)
        self.command_thread.finished.connect(self.command_thread.deleteLater)

        self.command_thread.start()

    def on_command_finished(self, output):
        self.progress_bar.setVisible(False)
        self.command_input.setEnabled(True)
        self.command_input.setFocus()

        dialog = OutputDialog(self, "Command Output", output)
        dialog.exec()

        if self.current_mode == 'all':
            self.check_all_packages()
        elif self.current_mode == 'outdated':
            self.check_outdated_packages()

    def on_command_error(self, error_message):
        self.progress_bar.setVisible(False)
        self.command_input.setEnabled(True)
        self.command_input.setFocus()

        dialog = OutputDialog(self, "Command Error", error_message)
        dialog.exec()

    def open_context_menu(self, position: QPoint):
        selected_row = self.results_table.currentRow()
        if selected_row < 0:
            return
        package_item = self.results_table.item(selected_row, 0)
        if not package_item:
            return
        package_name = package_item.text()
        menu = QMenu(self)
        upgrade_action = menu.addAction("Upgrade/Downgrade")
        upgrade_action.triggered.connect(lambda: self.fetch_versions(package_name, position))
        info_action = menu.addAction("View Package Info")
        info_action.triggered.connect(lambda: self.show_package_info(package_name))
        compare_deps_action = menu.addAction("Compare Dependencies")
        compare_deps_action.triggered.connect(lambda: self.compare_dependencies(package_name))
        menu.exec(self.results_table.viewport().mapToGlobal(position))

    def fetch_versions(self, package_name, position):
        self.package_name_to_upgrade = package_name
        self.position_for_menu = position

        self.thread_versions = QThread()
        self.worker_versions = VersionsWorker(package_name)
        self.worker_versions.moveToThread(self.thread_versions)
        self.thread_versions.started.connect(self.worker_versions.run)
        self.worker_versions.finished.connect(self.on_versions_fetched)
        self.worker_versions.error.connect(self.on_versions_error)
        self.worker_versions.finished.connect(self.thread_versions.quit)
        self.worker_versions.finished.connect(self.worker_versions.deleteLater)
        self.worker_versions.error.connect(self.thread_versions.quit)
        self.worker_versions.error.connect(self.worker_versions.deleteLater)
        self.thread_versions.finished.connect(self.thread_versions.deleteLater)
        self.thread_versions.start()

    def on_versions_fetched(self, versions):
        self.show_versions_menu(self.package_name_to_upgrade, versions, self.position_for_menu)

    def on_versions_error(self, error_message):
        self.show_message("Error", f"Error while fetching versions: {error_message}")

    def show_versions_menu(self, package_name, versions, position):
        if not versions:
            self.show_message("No Versions Found", f"No available versions found for '{package_name}'.")
            return

        menu = QMenu(self)
        for ver, release_date in reversed(versions):
            action_text = f"{ver} ({release_date})"
            try:
                if version.parse(ver).is_prerelease:
                    action_text += "  [pre-release]"
            except InvalidVersion:
                pass
            action = menu.addAction(action_text)
            action.triggered.connect(lambda checked, v=ver: self.upgrade_downgrade_package(package_name, v))

        menu.exec(self.results_table.viewport().mapToGlobal(position))

    def upgrade_downgrade_package(self, package_name, selected_version):
        reply = QMessageBox.question(
            self,
            "Confirm Upgrade/Downgrade",
            f"Are you sure you want to install version {selected_version} of '{package_name}'?\n\nThis will not install dependencies.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.No:
            return

        self.progress_bar.setVisible(True)
        self.results_table.setEnabled(False)
        self.package_name_being_updated = package_name
        self.selected_version = selected_version

        self.thread_pip = QThread()
        self.worker_pip = PipWorker(self.python_executable, package_name, selected_version)
        self.worker_pip.moveToThread(self.thread_pip)

        self.thread_pip.started.connect(self.worker_pip.run)
        self.worker_pip.finished.connect(self.on_pip_finished)
        self.worker_pip.error.connect(self.on_pip_error)
        self.worker_pip.finished.connect(self.thread_pip.quit)
        self.worker_pip.error.connect(self.thread_pip.quit)
        self.worker_pip.finished.connect(self.worker_pip.deleteLater)
        self.worker_pip.error.connect(self.worker_pip.deleteLater)
        self.thread_pip.finished.connect(self.thread_pip.deleteLater)

        self.thread_pip.start()

    def on_pip_finished(self, output):
        self.progress_bar.setVisible(False)
        self.results_table.setEnabled(True)

        dialog = OutputDialog(self, "Installation Complete",
                             f"Package '{self.package_name_being_updated}' installed successfully.\n\n{output}")
        dialog.exec()

        if self.current_mode == 'outdated':
            self.check_outdated_packages()
        elif self.current_mode == 'all':
            self.check_all_packages()

    def on_pip_error(self, error_message):
        self.progress_bar.setVisible(False)
        self.results_table.setEnabled(True)
        self.show_message("Error", f"Error while upgrading/downgrading '{self.package_name_being_updated}':\n{error_message}")

    def compare_dependencies(self, package_name):
        self.progress_bar.setVisible(True)
        self.package_to_compare = package_name

        self.compare_thread = QThread()
        self.compare_worker = CompareDependenciesWorker(self.python_executable, package_name)
        self.compare_worker.moveToThread(self.compare_thread)
        self.compare_thread.started.connect(self.compare_worker.run)
        self.compare_worker.finished.connect(self.on_compare_dependencies_finished)
        self.compare_worker.error.connect(self.on_compare_dependencies_error)
        self.compare_worker.finished.connect(self.compare_thread.quit)
        self.compare_worker.finished.connect(self.compare_worker.deleteLater)
        self.compare_worker.error.connect(self.compare_thread.quit)
        self.compare_worker.error.connect(self.compare_worker.deleteLater)
        self.compare_thread.finished.connect(self.compare_thread.deleteLater)
        self.compare_thread.start()

    def on_compare_dependencies_finished(self, current_version, latest_version, current_deps, latest_deps):
        self.progress_bar.setVisible(False)

        dialog = CompareDependenciesDialog(
            self,
            self.package_to_compare,
            current_version,
            latest_version,
            current_deps,
            latest_deps
        )
        dialog.exec()

    def on_compare_dependencies_error(self, error_message):
        self.progress_bar.setVisible(False)
        self.show_message("Error", f"Error comparing dependencies: {error_message}")

    def show_package_info(self, package_name):
        self.progress_bar.setVisible(True)
        self.package_info_name = package_name

        self.package_info_thread = QThread()
        self.package_info_worker = PackageInfoWorker(package_name)
        self.package_info_worker.moveToThread(self.package_info_thread)
        self.package_info_thread.started.connect(self.package_info_worker.run)
        self.package_info_worker.finished.connect(self.on_package_info_finished)
        self.package_info_worker.error.connect(self.on_package_info_error)
        self.package_info_worker.finished.connect(self.package_info_thread.quit)
        self.package_info_worker.finished.connect(self.package_info_worker.deleteLater)
        self.package_info_worker.error.connect(self.package_info_thread.quit)
        self.package_info_worker.error.connect(self.package_info_worker.deleteLater)
        self.package_info_thread.finished.connect(self.package_info_thread.deleteLater)
        self.package_info_thread.start()

    def on_package_info_finished(self, info):
        self.progress_bar.setVisible(False)
        package_name = self.package_info_name
        description = info.get('summary', 'No description available.')
        author = info.get('author', 'N/A')
        homepage = info.get('home_page', 'N/A')
        package_url = info.get('package_url', f"https://pypi.org/project/{package_name}/")
        project_urls = info.get('project_urls') or {}
        documentation = project_urls.get('Documentation', 'N/A')
        info_dialog = QDialog(self)
        info_dialog.setWindowTitle(f"Package Info: {package_name}")
        layout = QVBoxLayout(info_dialog)
        layout.addWidget(QLabel(f"<b>Package:</b> {package_name}"))
        layout.addWidget(QLabel(f"<b>Author:</b> {author}"))
        homepage_label = QLabel(f"<b>Homepage:</b> <a href='{homepage}'>{homepage}</a>")
        homepage_label.setTextFormat(Qt.RichText)
        homepage_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        homepage_label.setOpenExternalLinks(True)
        layout.addWidget(homepage_label)
        pypi_label = QLabel(f"<b>PyPI Page:</b> <a href='{package_url}'>{package_url}</a>")
        pypi_label.setTextFormat(Qt.RichText)
        pypi_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        pypi_label.setOpenExternalLinks(True)
        layout.addWidget(pypi_label)
        if documentation != 'N/A':
            doc_label = QLabel(f"<b>Documentation:</b> <a href='{documentation}'>{documentation}</a>")
            doc_label.setTextFormat(Qt.RichText)
            doc_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
            doc_label.setOpenExternalLinks(True)
            layout.addWidget(doc_label)
        description_label = QLabel(f"<b>Description:</b> {description}")
        description_label.setWordWrap(True)
        layout.addWidget(description_label)
        info_dialog.setLayout(layout)
        info_dialog.exec()

    def on_package_info_error(self, error_message):
        self.progress_bar.setVisible(False)
        self.show_message("Error", error_message)

    def set_tooltip_for_package(self, row, package_name):
        dep_info = self.dependencies_map.get(package_name, {})
        requires = dep_info.get('requires', [])
        required_by = dep_info.get('required_by', [])
        requires_text = ", ".join(requires) if requires else "None"
        required_by_text = ", ".join(required_by) if required_by else "None"
        tooltip_text = f"Requires: {requires_text}\nRequired by: {required_by_text}"
        package_item = self.results_table.item(row, 0)
        if package_item:
            package_item.setToolTip(tooltip_text)

    def show_message(self, title, message):
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(message)
        icon = QMessageBox.Information if title != "Error" else QMessageBox.Critical
        msg_box.setIcon(icon)
        msg_box.exec()


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = PackageChecker()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
