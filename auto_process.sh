#!/bin/zsh
# LaunchAgent entry point for the T-Mobile bill -> Zelle pipeline.
#
# All idempotency and stage logic lives in app.py (see ~/.tmo_state/).
# This script just sets up paths, logs the run, and invokes the orchestrator.
# The pipeline is safe to re-run on every scheduled day - it will exit early
# if the bill isn't posted yet, was already processed, or Zelle is already paid.

# REPO_DIR can be overridden via env. Defaults to a sibling of $HOME.
# When run by launchd, set the desired path via the LaunchAgent's
# EnvironmentVariables key, or by editing the line below.
REPO_DIR="${TMO_REPO_DIR:-$HOME/git_repo/tmo}"
PYTHON_PATH="${TMO_PYTHON:-$REPO_DIR/tmobile_env/bin/python}"
LOG_FILE="$REPO_DIR/automation.log"

cd "$REPO_DIR" || exit 1

echo "--- Automation Started: $(date) ---" >> "$LOG_FILE"

# caffeinate -i holds off idle sleep for the entire run, so an unattended run
# can't be cut short mid-flight (e.g. during the up-to-5-minute BoA MFA wait).
# It releases automatically when app.py exits, so normal idle-sleep resumes.
caffeinate -i "$PYTHON_PATH" -u "$REPO_DIR/app.py" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "--- Automation Completed Successfully: $(date) ---" >> "$LOG_FILE"
else
    echo "--- Automation Exited With Code $EXIT_CODE: $(date) ---" >> "$LOG_FILE"
fi

# Refresh the local monthly transactions dashboard (best-effort; runs AFTER the
# pipeline so it never affects the run or its exit code). Keeps dashboard.html
# current after every scheduled run with no manual step.
"$PYTHON_PATH" -u "$REPO_DIR/dashboard.py" >> "$LOG_FILE" 2>&1 || true

exit $EXIT_CODE
