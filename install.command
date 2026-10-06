#!/bin/zsh
set -eu
cd -- "${0:A:h}"
python_bin="${SCHEDULEX_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
  for candidate in /opt/homebrew/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/local/bin/python3; do
    if [[ -x "$candidate" ]] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
      python_bin="$candidate"
      break
    fi
  done
fi
if [[ -z "$python_bin" ]]; then
  python_bin="$(command -v python3 || true)"
fi
if [[ -z "$python_bin" ]] || ! "$python_bin" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo 'Schedulex requires Python 3.11 or newer. Install Python, then run this installer again.'
  exit 1
fi
"$python_bin" install.py "$@"
