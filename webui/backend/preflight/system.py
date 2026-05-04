import shutil
import sys
import platform
from ._common import CheckResult, PreflightResult, aggregate


def check() -> PreflightResult:
    checks: list[CheckResult] = []

    # Python
    if sys.version_info >= (3, 10):
        checks.append(CheckResult(name="python", status="ok",
                                  message=f"Python {sys.version.split()[0]}"))
    else:
        checks.append(CheckResult(name="python", status="fail",
                                  message=f"Python {sys.version.split()[0]} < 3.10"))

    # Binaries
    for binary in ("camoufox",):
        path = shutil.which(binary)
        checks.append(CheckResult(
            name=binary,
            status="ok" if path else "fail",
            message=path or f"{binary} not found in PATH",
        ))

    xvfb_path = shutil.which("xvfb-run")
    if xvfb_path:
        checks.append(CheckResult(name="xvfb-run", status="ok", message=xvfb_path))
    elif platform.system() == "Windows":
        checks.append(CheckResult(
            name="xvfb-run",
            status="warn",
            message="Windows does not use xvfb-run; WebUI will run pipeline.py directly",
        ))
    else:
        checks.append(CheckResult(
            name="xvfb-run",
            status="fail",
            message="xvfb-run not found in PATH",
        ))

    # Playwright import
    try:
        import playwright  # noqa: F401
        checks.append(CheckResult(name="playwright", status="ok",
                                  message="playwright importable"))
    except ImportError as e:
        checks.append(CheckResult(name="playwright", status="fail",
                                  message=str(e)))

    return aggregate(checks)
