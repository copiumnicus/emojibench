#!/bin/sh
# Runs emojibench.py in a throwaway container that sees only this folder and your OpenRouter key:
#   ./sandbox.sh bench -m openrouter:z-ai/glm-5.3@baidu/fp8
# Claude Code isn't in the container, so the judge (Claude Haiku 5.5) runs through OpenRouter.
cd "$(dirname "$0")" || exit 1
[ -t 0 ] && tty=-it
exec docker run --rm $tty --user "$(id -u):$(id -g)" -v "$PWD:/bench" -w /bench \
  -e OPENROUTER_API_KEY -e OPENROUTER_BASE_URL python:3.12-slim python3 emojibench.py "$@"
