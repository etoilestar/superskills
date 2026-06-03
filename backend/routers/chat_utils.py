"""Shared chat utilities for sandbox and creator flows."""

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from ..config import settings
from ..services.model_router import route_model, PLANNER_TASK, VALIDATOR_TASK
from ..services.skill_governance import allowed_skill_roots
from .chat_models import ChatRequest, MarkdownBlock

logger = logging.getLogger(__name__)

# Explicit confirmation keywords for gating flows.
_CONFIRM_KEYWORDS = ("对，开始做吧", "开始制作", "开始干吧")

# 通用 fenced block 抽取器：用于解析所有 ```lang 代码块（执行与写文件统一入口）
_ALL_FENCE_RE = re.compile(
    r"(?P<fence>`{3,})(?P<info>[^\n`]*)\n(?P<code>[\s\S]*?)\n(?=\1)",
    re.IGNORECASE | re.DOTALL,
)

_PYTHON_HEREDOC_RE = re.compile(
    r"^\s*(?P<python>python3?|[\w./-]*python3?)\s+-\s+<<[ \t]*['\"]?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)['\"]?[ \t]*\n"
    r"(?P<script>.*?)\n(?P=tag)[ \t]*;?\s*$",
    re.DOTALL,
)

_ALLOWED_PLAN_ACTIONS = {"display", "ignore", "write_file", "run_command", "create_directory"}

# 脚本扩展名 → 自动注入的解释器（方案 A+B）
# 注意：".ts" 是特殊情况，实际命令为 `npx ts-node <file>`，见 _prepare_command_argv。
_SCRIPT_INTERPRETERS: dict[str, str] = {
    ".js":   "node",
    ".mjs":  "node",
    ".cjs":  "node",
    ".py":   "python3",
    ".sh":   "bash",
    ".bash": "bash",
    ".rb":   "ruby",
    ".ts":   "ts-node",   # 特殊处理：通过 `npx ts-node` 执行
}

# 解释器 → apt 包名映射（运行时自动安装兜底）
_INTERPRETER_APT_PACKAGES: dict[str, str] = {
    "node":    "nodejs",
    "nodejs":  "nodejs",
    "npm":     "npm",
    "npx":     "npm",      # npx 随 npm 一起安装
    "ts-node": "ts-node",  # 通过 npm 全局安装，见下方特殊处理
    "ruby":    "ruby",
    "bash":    "bash",
    "python3": "python3",
}

# 已尝试自动安装的解释器集合（避免同一进程内重复安装）
_auto_install_attempted: set[str] = set()
_auto_install_lock = __import__("threading").Lock()
_apt_update_performed: bool = False

# Python import 名 → pip 包名映射表（约 70 条常用别名）
_IMPORT_TO_PACKAGE: dict[str, str] = {
    "cv2":           "opencv-python",
    "PIL":           "Pillow",
    "pptx":          "python-pptx",
    "sklearn":       "scikit-learn",
    "bs4":           "beautifulsoup4",
    "yaml":          "pyyaml",
    "dotenv":        "python-dotenv",
    "magic":         "python-magic",
    "usb":           "pyusb",
    "serial":        "pyserial",
    "dateutil":      "python-dateutil",
    "boto3":         "boto3",
    "botocore":      "botocore",
    "paramiko":      "paramiko",
    "cryptography":  "cryptography",
    "nacl":          "pynacl",
    "Crypto":        "pycryptodome",
    "OpenSSL":       "pyOpenSSL",
    "jwt":           "PyJWT",
    "aiohttp":       "aiohttp",
    "httpx":         "httpx",
    "requests":      "requests",
    "flask":         "Flask",
    "fastapi":       "fastapi",
    "uvicorn":       "uvicorn",
    "django":        "Django",
    "sqlalchemy":    "SQLAlchemy",
    "pymysql":       "PyMySQL",
    "psycopg2":      "psycopg2-binary",
    "redis":         "redis",
    "pymongo":       "pymongo",
    "celery":        "celery",
    "pika":          "pika",
    "kafka":         "kafka-python",
    "numpy":         "numpy",
    "pandas":        "pandas",
    "matplotlib":    "matplotlib",
    "scipy":         "scipy",
    "tensorflow":    "tensorflow",
    "torch":         "torch",
    "transformers":  "transformers",
    "tqdm":          "tqdm",
    "rich":          "rich",
    "click":         "click",
    "typer":         "typer",
    "loguru":        "loguru",
    "pydantic":      "pydantic",
    "toml":          "toml",
    "arrow":         "arrow",
    "pendulum":      "pendulum",
    "docx":          "python-docx",
    "openpyxl":      "openpyxl",
    "xlrd":          "xlrd",
    "xlwt":          "xlwt",
    "pypdf":         "pypdf",
    "fitz":          "PyMuPDF",
    "jinja2":        "Jinja2",
    "markdown":      "Markdown",
    "lxml":          "lxml",
    "pytest":        "pytest",
    "faker":         "Faker",
    "hypothesis":    "hypothesis",
    "parameterized": "parameterized",
}

# Node.js 内置模块集合（过滤用，不安装这些模块）
_NODE_BUILTIN_MODULES: frozenset[str] = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster",
    "console", "constants", "crypto", "dgram", "diagnostics_channel",
    "dns", "domain", "events", "fs", "http", "http2", "https",
    "inspector", "module", "net", "os", "path", "perf_hooks",
    "process", "punycode", "querystring", "readline", "repl",
    "stream", "string_decoder", "sys", "timers", "tls", "trace_events",
    "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
    "node:assert", "node:async_hooks", "node:buffer", "node:child_process",
    "node:cluster", "node:console", "node:crypto", "node:dgram",
    "node:dns", "node:domain", "node:events", "node:fs", "node:http",
    "node:http2", "node:https", "node:inspector", "node:module",
    "node:net", "node:os", "node:path", "node:perf_hooks", "node:process",
    "node:readline", "node:repl", "node:stream", "node:string_decoder",
    "node:timers", "node:tls", "node:tty", "node:url", "node:util",
    "node:v8", "node:vm", "node:worker_threads", "node:zlib",
})

# 已创建 venv 的 skill 目录缓存（进程级，避免重复创建）
_venv_created_dirs: set[str] = set()
_venv_created_lock = __import__("threading").Lock()

# run_command 依赖缺失时最多重试次数
_MAX_DEP_RETRY = 3

# 目录快照时跳过的子目录名（虚拟环境、包缓存等）
_SNAPSHOT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".venv", "node_modules", "__pycache__", ".runtime", ".git",
})


def _last_user_text(request: ChatRequest) -> str:
    """Return the latest user utterance (or empty string)."""
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content or ""
    return ""


def _has_creation_confirmation(request: ChatRequest) -> bool:
    """Return True when the user confirms creation explicitly."""
    text = _last_user_text(request).strip()
    return any(keyword in text for keyword in _CONFIRM_KEYWORDS)


def _allowed_skill_roots() -> list[Path]:
    """Return directories under which the executor may create or modify files."""
    roots = [root.expanduser().resolve() for root in allowed_skill_roots()]

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)

    return deduped


def _is_within_sandbox(entry: Path, sandbox_root: Path) -> bool:
    """Return True only when *entry* resolves to a path inside *sandbox_root*.

    Rejects symlinks that point outside the skill sandbox, preventing a
    malicious skill from exposing files such as /etc/passwd via read_resource.
    """
    try:
        entry.resolve().relative_to(sandbox_root)
        return True
    except ValueError:
        return False


def _snapshot_dir_files(path: Path) -> set[str]:
    """Return a set of relative POSIX paths for all files under *path*.

    Directories in _SNAPSHOT_EXCLUDE_DIRS are skipped to avoid scanning
    virtual-env trees or node_modules.
    """
    result: set[str] = set()
    if not path.exists():
        return result
    for f in path.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(path)
        except ValueError:
            continue
        if any(part in _SNAPSHOT_EXCLUDE_DIRS for part in rel.parts):
            continue
        result.add(rel.as_posix())
    return result


def _try_auto_install_interpreter(interpreter: str) -> bool:
    """尝试通过系统包管理器（apt-get）自动安装缺失的解释器。

    只在 Linux 环境且具备 apt-get 时生效；安装结果会被缓存，
    同一进程内同一解释器只尝试安装一次。
    返回 True 表示安装后解释器可用，False 表示安装失败或不支持。
    """
    global _apt_update_performed

    with _auto_install_lock:
        if interpreter in _auto_install_attempted:
            return shutil.which(interpreter) is not None
        _auto_install_attempted.add(interpreter)

    apt_get = shutil.which("apt-get")
    if apt_get is None:
        logger.info("auto-install: apt-get not available, skipping install of %s", interpreter)
        return False

    # ts-node 通过 npm 全局安装，而不是 apt-get
    if interpreter == "ts-node":
        npm_bin = shutil.which("npm")
        if npm_bin is None:
            logger.warning("auto-install: npm not found, cannot install ts-node")
            return False
        logger.info("auto-install: installing ts-node via npm ...")
        try:
            result = subprocess.run(
                [npm_bin, "install", "-g", "ts-node", "typescript"],
                timeout=120,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.info("auto-install: ts-node installed successfully")
            else:
                logger.warning("auto-install: ts-node install failed: %s", result.stderr[:500])
        except Exception as exc:
            logger.warning("auto-install: ts-node install exception: %s", exc)
        return shutil.which("ts-node") is not None

    pkg = _INTERPRETER_APT_PACKAGES.get(interpreter)
    if pkg is None:
        logger.info("auto-install: no apt package known for interpreter %s", interpreter)
        return False

    logger.info("auto-install: apt-get install -y %s (for interpreter %s) ...", pkg, interpreter)
    try:
        # 先更新索引（只在当次进程首次 apt 安装时执行）
        if not _apt_update_performed:
            subprocess.run(
                [apt_get, "update", "-qq"],
                timeout=60,
                capture_output=True,
                check=False,
            )
            _apt_update_performed = True

        result = subprocess.run(
            [apt_get, "install", "-y", "--no-install-recommends", pkg],
            timeout=120,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info("auto-install: %s installed successfully", pkg)
        else:
            logger.warning("auto-install: install failed (rc=%d): %s", result.returncode, result.stderr[:500])
    except subprocess.TimeoutExpired:
        logger.warning("auto-install: apt-get install %s timed out", pkg)
    except Exception as exc:
        logger.warning("auto-install: apt-get install %s exception: %s", pkg, exc)

    return shutil.which(interpreter) is not None


# ---------------------------------------------------------------------------
# Per-skill isolated environment helpers
# ---------------------------------------------------------------------------

def _get_skill_venv_python(skill_dir: Path) -> Path:
    """Ensure skill_dir/.venv exists and return its python executable path.

    Uses a process-level cache so the venv is created at most once per skill
    per process lifetime.
    """
    venv_dir = skill_dir / ".venv"
    venv_python = venv_dir / "bin" / "python"
    key = str(skill_dir.resolve())

    with _venv_created_lock:
        if key not in _venv_created_dirs:
            if not venv_dir.exists():
                logger.info("skill-env: creating venv at %s", venv_dir)
                result = subprocess.run(
                    ["python3", "-m", "venv", str(venv_dir)],
                    timeout=60,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"创建 skill venv 失败 ({venv_dir}): {result.stderr[:500]}"
                    )
            _venv_created_dirs.add(key)

    return venv_python


def _scan_and_install_python_deps(script_path: Path, venv_python: Path) -> None:
    """Static-scan a .py script and pip-install any missing third-party imports
    into the per-skill venv before the script is executed.
    """
    import ast
    import sys

    try:
        source = script_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(script_path))
    except SyntaxError:
        return  # 语法错误留给执行时报告

    top_level_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_names.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_level_names.append(node.module.split(".")[0])

    stdlib_names: frozenset[str] = frozenset(getattr(sys, "stdlib_module_names", set()))

    to_install: list[str] = []
    seen: set[str] = set()
    for name in top_level_names:
        if not name or name.startswith("_") or name == "backend" or name in stdlib_names or name in seen:
            continue
        # Only proceed if the name is a safe identifier (AST-sourced, but be explicit)
        if not name.isidentifier():
            continue
        seen.add(name)
        pkg = _IMPORT_TO_PACKAGE.get(name, name)
        # Use importlib.util.find_spec via argv rather than f-string interpolation
        check = subprocess.run(
            [
                str(venv_python),
                "-c",
                "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 1)",
                name,
            ],
            capture_output=True,
            timeout=10,
        )
        if check.returncode != 0:
            to_install.append(pkg)

    if to_install:
        logger.info("skill-env: pip installing into venv: %s", to_install)
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--quiet"] + to_install,
            timeout=180,
            capture_output=True,
            text=True,
        )


def _scan_and_install_node_deps(script_path: Path, skill_dir: Path) -> None:
    """Static-scan a .js/.mjs script and npm-install any missing third-party
    modules into the per-skill node_modules before the script is executed.

    策略（按优先级）：
    1. 解析 skill_dir/package.json 的 dependencies / devDependencies。
    2. 递归跟踪本地 require('./xxx.js') / import './xxx.js' 引用（最多 2 层深）。
    3. 正则扫描主脚本及所有被递归发现的子脚本中的第三方 import/require。
    """
    npm_bin = shutil.which("npm")
    if not npm_bin:
        return

    import_patterns = [
        # CommonJS: require('pkg') / require("pkg")
        re.compile(r"""require\s*\(\s*['"]([^'"./][^'"]*)['"]\s*\)"""),
        # ES module: import ... from 'pkg'
        re.compile(r"""from\s+['"]([^'"./][^'"]*)['"]\s*"""),
        # Side-effect import: import 'pkg'
        re.compile(r"""import\s+['"]([^'"./][^'"]*)['"]\s*"""),
        # Dynamic import: import('pkg')
        re.compile(r"""import\s*\(\s*['"]([^'"./][^'"]*)['"]\s*\)"""),
    ]
    local_require_pattern = re.compile(
        r"""require\s*\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)|"""
        r"""from\s+['"](\.{1,2}/[^'"]+)['"]\s*""",
    )

    def _collect_third_party(source: str) -> list[str]:
        found: list[str] = []
        for pat in import_patterns:
            for m in pat.finditer(source):
                raw = m.group(1)
                if raw.startswith("@"):
                    parts = raw.split("/")
                    name = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
                else:
                    name = raw.split("/")[0]
                if name and name not in found:
                    found.append(name)
        return found

    def _collect_local_refs(source: str, base: Path, skill_root: Path) -> list[Path]:
        refs: list[Path] = []
        for m in local_require_pattern.finditer(source):
            rel = m.group(1) or m.group(2)
            if not rel:
                continue
            candidate = (base / rel).resolve()
            if not candidate.suffix:
                candidate = candidate.with_suffix(".js")
            # Security: only follow references that stay inside the skill directory
            try:
                candidate.relative_to(skill_root)
            except ValueError:
                continue
            if candidate.is_file():
                refs.append(candidate)
        return refs

    names: list[str] = []
    skill_root = skill_dir.resolve()

    # 1. Parse package.json if present
    pkg_json = skill_root / "package.json"
    if pkg_json.is_file():
        try:
            pkg_data = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
            for section in ("dependencies", "devDependencies"):
                for pkg_name in (pkg_data.get(section) or {}).keys():
                    if pkg_name and pkg_name not in names:
                        names.append(pkg_name)
        except (json.JSONDecodeError, OSError):
            pass

    # 2 + 3. Recursively scan entry script and local requires (up to 2 levels deep)
    visited: set[Path] = set()
    queue: list[tuple[Path, int]] = [(script_path.resolve(), 0)]
    while queue:
        current, depth = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        try:
            source = current.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pkg_name in _collect_third_party(source):
            if pkg_name not in names:
                names.append(pkg_name)
        if depth < 2:
            for child in _collect_local_refs(source, current.parent, skill_root):
                if child not in visited:
                    queue.append((child, depth + 1))

    to_install = [
        n for n in names
        if n not in _NODE_BUILTIN_MODULES
        and not (skill_dir / "node_modules" / n).exists()
    ]

    if to_install:
        logger.info("skill-env: npm installing into %s: %s", skill_dir, to_install)
        subprocess.run(
            [npm_bin, "install", "--prefix", str(skill_dir), "--quiet"] + to_install,
            timeout=180,
            capture_output=True,
            text=True,
            cwd=str(skill_dir),
        )


def _retry_install_python_dep(module_name: str, venv_python: Path) -> bool:
    """Error-driven: install a single missing Python module into the skill venv.

    Returns True if the install command succeeded.
    """
    pkg = _IMPORT_TO_PACKAGE.get(module_name, module_name)
    logger.info("skill-env: error-driven pip install %s (for import %s)", pkg, module_name)
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", pkg],
        timeout=180,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _retry_install_node_dep(module_name: str, skill_dir: Path) -> bool:
    """Error-driven: install a single missing Node.js module into skill node_modules.

    Returns True if the install command succeeded.
    """
    npm_bin = shutil.which("npm")
    if not npm_bin:
        return False
    logger.info("skill-env: error-driven npm install %s into %s", module_name, skill_dir)
    result = subprocess.run(
        [npm_bin, "install", "--prefix", str(skill_dir), "--quiet", module_name],
        timeout=180,
        capture_output=True,
        text=True,
        cwd=str(skill_dir),
    )
    return result.returncode == 0


def _friendly_error(exc: Exception) -> str:
    """Convert LLM proxy exceptions to user-facing messages without leaking internals."""
    if isinstance(exc, httpx.ConnectError):
        return "无法连接到 LLM 服务，请确认 Ollama 已启动，且端口可访问"

    if isinstance(exc, httpx.HTTPStatusError):
        return f"LLM 服务返回错误: HTTP {exc.response.status_code}"

    if isinstance(exc, httpx.TimeoutException):
        return "LLM 服务响应超时，请重试"

    return "生成时发生错误，请重试"


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _quick_actions(actions: list[dict]) -> str:
    """Build a 'quick_actions' SSE event that carries button suggestions for user to click.
    
    Each action should have:
      - text: button text to display
      - value: text to send when clicked
      - style: optional, default "default", can be "primary", "danger", etc.
    """
    return _sse({
        "quick_actions": {
            "actions": actions,
            "ts": time.time(),
        }
    })


def _thought(step: str, label: str, detail: str, data: dict | None = None) -> str:
    """Build a 'thought' SSE event that carries internal decision/execution data.

    step values:
      metadata_decision | body_loaded | child_decision | resource_selection |
      planner_output | action_start | action_result | final_answer
    """
    return _sse({
        "thought": {
            "step": step,
            "label": label,
            "detail": detail,
            "data": data or {},
            "ts": time.time(),
        }
    })


def _expand_arg_env_vars(arg: str, env: dict) -> str:
    """Expand shell-style $VAR / ${VAR} references in a single command argument.

    Uses the supplied *env* dict rather than the live process environment so
    that values injected by the skill runtime (OUTPUT_DIR, INPUT_DIR, …) are
    always honoured even when subprocess uses shell=False.
    """
    if "$" not in arg:
        return arg
    result = arg
    # Replace ${VAR} first — unambiguous because of the braces.
    for var, val in env.items():
        result = result.replace(f"${{{var}}}", val)
    # Replace bare $VAR in decreasing name-length order so that a
    # shorter name that is a prefix of a longer one (e.g. INPUT_DIR
    # vs INPUT_SESSION_DIR) never clobbers the longer match.
    for var in sorted(env, key=len, reverse=True):
        result = result.replace(f"${var}", env[var])
    return result


def _request_messages(request: ChatRequest) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in request.messages]


def _request_messages_with_files(request: ChatRequest) -> list[dict]:
    """Like _request_messages, but appends a compact file-attachment note to the
    last user message so the LLM sees the files as part of the conversation turn
    rather than only in the system prompt.
    """
    if not request.input_files:
        return _request_messages(request)

    messages = [{"role": m.role, "content": m.content} for m in request.messages]
    # Find the last user message and append a file-attachment note.
    for i in reversed(range(len(messages))):
        if messages[i]["role"] == "user":
            names = ", ".join(
                f.get("filename") or Path(f.get("path", "")).name
                for f in request.input_files
            )
            messages[i] = {
                "role": "user",
                "content": messages[i]["content"] + f"\n\n【已附上传文件：{names}】",
            }
            break
    return messages


def _extract_input_session_dir(input_files: list[dict], execution_root: "Path | None") -> "Path | None":
    """Derive the session-specific input directory from the first uploaded file's path.

    Uploaded files are stored at  inputs/<session_id>/<filename>  relative to the
    skill root.  This helper resolves the absolute  inputs/<session_id>/  path so
    subprocesses can receive it as INPUT_SESSION_DIR, allowing scripts to discover
    all uploaded files without hard-coding the session ID.
    """
    if not input_files or execution_root is None:
        return None
    first_path = input_files[0].get("path", "")
    parts = Path(first_path).parts  # ("inputs", "<session_id>", "<filename>")
    if len(parts) >= 2 and parts[0] == "inputs":
        return execution_root / "inputs" / parts[1]
    return None


def _rewrite_argv_input_paths(
    argv: list[str],
    input_files: list[dict],
    execution_root: "Path | None",
    session_input_dir: "Path | None",
) -> list[str]:
    """Rewrite argv elements that reference uploaded files to absolute paths.

    The LLM may generate file arguments using conventions like:
      - ``uploads/<filename>``
      - a bare ``<filename>`` that matches an uploaded file

    These won't resolve when the subprocess runs with cwd=skill_root or
    cwd=scripts/.  This helper replaces such arguments with the real absolute
    path so the script can open the file.

    When ``uploads/<name>`` is used but ``<name>`` does not match any uploaded
    file exactly (e.g. the LLM copied a placeholder filename like ``data.xlsx``
    from a SKILL.md example), the function falls back to:
      1. The single uploaded file whose extension matches ``<name>``'s extension.
      2. ``session_input_dir/<name>`` so the subprocess CWD issue is at least
         resolved (the script itself will then report a meaningful error if the
         file is truly absent).
    """
    if not input_files or execution_root is None:
        return argv

    # Build a filename → absolute-path map from uploaded file records.
    filename_to_abs: dict[str, str] = {}
    for f in input_files:
        rel = f.get("path", "")
        fname = f.get("filename") or (Path(rel).name if rel else "")
        if rel and fname:
            abs_path = execution_root / rel
            filename_to_abs[fname] = str(abs_path)

    if not filename_to_abs:
        return argv

    # Build an extension → list-of-absolute-paths index for fuzzy fallback.
    # Used when the LLM copies a placeholder filename from SKILL.md (e.g.
    # "uploads/data.xlsx") but the user actually uploaded "report.xlsx".
    ext_to_abs: dict[str, list[str]] = {}
    for fname, abs_p in filename_to_abs.items():
        ext = Path(fname).suffix.lower()
        ext_to_abs.setdefault(ext, []).append(abs_p)

    result: list[str] = []
    for arg in argv:
        rewritten = arg
        # Pattern 1: uploads/<filename>  or  uploads\<filename>
        for prefix in ("uploads/", "uploads\\"):
            if rewritten.startswith(prefix):
                candidate = rewritten[len(prefix):]
                if candidate in filename_to_abs:
                    # Exact filename match — use the known absolute path.
                    rewritten = filename_to_abs[candidate]
                else:
                    # Exact match failed: the LLM may have copied a placeholder
                    # filename from the SKILL.md example.  Fall back to the
                    # only uploaded file whose extension matches the placeholder.
                    placeholder_ext = Path(candidate).suffix.lower()
                    matches = ext_to_abs.get(placeholder_ext, [])
                    if len(matches) == 1:
                        rewritten = matches[0]
                    elif session_input_dir is not None:
                        # Multiple (or zero) extension matches: redirect the
                        # directory portion to the session input dir and keep
                        # the original filename so the script can report a
                        # meaningful "file not found" error if needed.
                        rewritten = str(session_input_dir / candidate)
                break
        # Pattern 2: bare filename that exactly matches an upload (only when the
        # argument doesn't already look like an absolute or relative path).
        if (
            rewritten == arg  # not yet rewritten
            and "/" not in rewritten
            and "\\" not in rewritten
            and rewritten in filename_to_abs
        ):
            rewritten = filename_to_abs[rewritten]
        result.append(rewritten)
    return result


def _strip_markdown_json_fence(text: str) -> str:
    """Remove common markdown code fences around JSON.

    Handles three cases in order:
    1. The whole response is a ```json ... ``` fence (most common for well-behaved models).
    2. The whole response is a bare ``` ... ``` fence.
    3. The JSON fence is embedded inside a longer natural-language response — the model
       added prose before/after the JSON block.  In this case we search for the first
       ```json ... ``` or ``` ... ``` block whose content starts with ``{`` or ``[``.
    4. Last resort: find the first ``{`` or ``[`` that could start a JSON object/array.
    """
    stripped = text.strip()

    # Case 1 & 2: fence at the start of the response.
    if stripped.startswith("```json"):
        stripped = stripped[len("```json"):].strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
        return stripped

    if stripped.startswith("```"):
        stripped = stripped[len("```"):].strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
        # Only return early if the result looks like JSON.
        if stripped.startswith("{") or stripped.startswith("["):
            return stripped

    # If the text already looks like JSON, return it directly.
    if stripped.startswith("{") or stripped.startswith("["):
        return stripped

    # Case 3: embedded ```json ... ``` block anywhere in the text.
    m = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if m:
        candidate = m.group(1).strip()
        if candidate.startswith("{") or candidate.startswith("["):
            return candidate

    # Embedded ``` ... ``` block whose content looks like JSON.
    m = re.search(r"```\s*([\s\S]*?)\s*```", text)
    if m:
        candidate = m.group(1).strip()
        if candidate.startswith("{") or candidate.startswith("["):
            return candidate

    # Case 4: bare JSON object/array anywhere in the text.
    m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m:
        return m.group(1).strip()

    return stripped


def _normalize_fence_lang(info: str) -> str:
    """Return the first token of a Markdown fence info string."""
    info = (info or "").strip()
    if not info:
        return ""
    return info.split()[0].strip().lower()


def _extract_all_fenced_blocks(text: str, *, context_chars: int = 420) -> list[MarkdownBlock]:
    """Extract fenced code blocks using a line-based parser.

    支持外层 ````markdown 包含内部 ```bash 的情况。
    只有遇到长度 >= opening fence 的同字符 fence，才关闭当前 block。
    """
    blocks: list[MarkdownBlock] = []
    lines = text.splitlines(keepends=True)

    pos = 0
    i = 0
    block_index = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        indent_len = len(line) - len(stripped)

        match = re.match(r"(`{3,}|~{3,})([^\n`]*)\n?$", stripped.rstrip("\n"))
        if not match:
            pos += len(line)
            i += 1
            continue

        fence = match.group(1)
        fence_char = fence[0]
        fence_len = len(fence)
        info = match.group(2).strip()
        start_pos = pos
        code_start_pos = pos + len(line)

        code_lines: list[str] = []
        pos += len(line)
        i += 1

        closed = False
        while i < len(lines):
            close_line = lines[i]
            close_stripped = close_line.lstrip()

            close_match = re.match(
                rf"{re.escape(fence_char)}{{{fence_len},}}\s*$",
                close_stripped.rstrip("\n"),
            )

            if close_match:
                end_pos = pos + len(close_line)
                before = text[max(0, start_pos - context_chars):start_pos].strip()
                after = text[end_pos:min(len(text), end_pos + context_chars)].strip()

                blocks.append(
                    MarkdownBlock(
                        index=block_index,
                        lang=_normalize_fence_lang(info),
                        code="".join(code_lines).rstrip("\n"),
                        before_context=before,
                        after_context=after,
                    )
                )
                block_index += 1

                pos += len(close_line)
                i += 1
                closed = True
                break

            code_lines.append(close_line)
            pos += len(close_line)
            i += 1

        if not closed:
            # 未闭合代码块不执行，避免写入半截文件
            break

    return blocks


def _validate_skill_md(skill_md: Path) -> None:
    if not skill_md.exists():
        raise ValueError("缺少 SKILL.md 文件")

    text = skill_md.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md 缺少 YAML frontmatter")

    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md frontmatter 未正确闭合")

    frontmatter = parts[1]
    name_match = re.search(r"^name:\s*([a-z0-9-]+)\s*$", frontmatter, re.M)
    desc_match = re.search(r"^description:\s*(.+)\s*$", frontmatter, re.M)

    if not name_match:
        raise ValueError("frontmatter 缺少合法的 name 字段，只能使用小写字母、数字和连字符")

    if len(name_match.group(1)) > 64:
        raise ValueError("name 字段超过 64 个字符")

    if not desc_match or not desc_match.group(1).strip():
        raise ValueError("frontmatter 缺少 description 字段")


def _find_created_skill_roots(touched_paths: list[Path]) -> list[Path]:
    roots: set[Path] = set()
    allowed_roots = _allowed_skill_roots()

    for path in touched_paths:
        current = path if path.is_dir() else path.parent
        while current != current.parent:
            if (current / "SKILL.md").exists():
                roots.add(current)
                break
            if any(current.parent == root for root in allowed_roots):
                roots.add(current)
                break
            current = current.parent

    return sorted(roots)


def _blocks_for_planner(blocks: list[MarkdownBlock]) -> list[dict]:
    return [
        {
            "index": block.index,
            "lang": block.lang,
            "code_preview": block.code[:4000],
            "before_context": "\n".join(block.before_context.splitlines()[-8:]),
            "after_context": "\n".join(block.after_context.splitlines()[:4]),
        }
        for block in blocks
    ]


def _planner_model_name(default_model: str) -> str:
    """Select a separate planner model when configured.

    建议在 config 中增加 planner_model 或 action_planner_model。
    推荐部署方式：Ollama/OpenAI-compatible 接口，不建议在当前 FastAPI 进程内本地加载大模型。
    """
    return route_model(PLANNER_TASK, requested_model=default_model, reason="silent planner round").model


def _validator_model_name(default_model: str) -> str:
    """Select a separate validator model when configured.

    校验轮只需要 JSON 分类能力，小/快模型即可胜任。
    未配置时回退到 default_model。
    """
    return route_model(VALIDATOR_TASK, requested_model=default_model, reason="output validator round").model
