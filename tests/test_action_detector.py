"""Tests for the action detector."""
from nvh.core.action_detector import detect_action


class TestActionDetection:
    def test_install_package(self):
        a = detect_action("install pandas")
        assert a is not None
        assert a.tool_name == "pip_install"
        assert a.arguments["package"] == "pandas"
        assert a.requires_confirm

    def test_open_app(self):
        a = detect_action("open firefox")
        assert a is not None
        assert a.tool_name == "open"

    def test_open_url_adds_https(self):
        a = detect_action("open google.com")
        assert a is not None
        assert a.arguments["target"] == "https://google.com"

    def test_question_not_action(self):
        assert detect_action("what is machine learning") is None

    def test_question_with_mark_not_action(self):
        assert detect_action("how does CUDA work?") is None

    def test_explain_not_action(self):
        assert detect_action("explain quantum computing") is None

    def test_kill_process_by_name(self):
        a = detect_action("kill python")
        assert a is not None
        assert a.tool_name == "kill_process"
        assert a.requires_confirm

    def test_kill_process_by_pid(self):
        a = detect_action("kill 12345")
        assert a is not None
        assert a.arguments.get("pid") == 12345

    def test_disk_usage(self):
        a = detect_action("how much disk space")
        assert a is not None
        assert a.tool_name == "disk_usage"

    def test_find_files(self):
        a = detect_action("find large files")
        assert a is not None
        assert a.tool_name == "find_files"

    def test_system_info(self):
        a = detect_action("show system info")
        assert a is not None
        assert a.tool_name == "system_info"

    def test_list_processes(self):
        a = detect_action("what's running")
        assert a is not None
        assert a.tool_name == "list_processes"

    def test_open_terminal(self):
        a = detect_action("new terminal")
        assert a is not None
        assert a.tool_name == "open_terminal"

    def test_clipboard_read(self):
        a = detect_action("what's on my clipboard")
        assert a is not None
        assert a.tool_name == "get_clipboard"

    def test_download(self):
        a = detect_action("download https://example.com/file.zip")
        assert a is not None
        assert a.tool_name == "download"
        assert a.requires_confirm

    def test_delete_requires_confirm(self):
        a = detect_action("delete temp.txt")
        assert a is not None
        assert a.requires_confirm

    def test_move_file(self):
        a = detect_action("move report.pdf to ~/Documents")
        assert a is not None
        assert a.tool_name == "move_file"

    def test_visit_url(self):
        a = detect_action("go to nvidia.com")
        assert a is not None
        assert a.tool_name == "open"
        assert "nvidia.com" in a.arguments["target"]

    def test_launch_app(self):
        a = detect_action("launch code")
        assert a is not None
        assert a.tool_name == "open"

    def test_pip_list(self):
        a = detect_action("list python packages")
        assert a is not None
        assert a.tool_name == "pip_list"

    def test_notify(self):
        a = detect_action("notify me that the build is done")
        assert a is not None
        assert a.tool_name == "notify"

    # Ensure ambiguous cases go to LLM
    def test_write_code_is_question(self):
        assert detect_action("write a Python function") is None

    def test_help_me_is_question(self):
        assert detect_action("help me understand recursion") is None

    def test_compare_is_question(self):
        assert detect_action("compare React and Vue") is None
