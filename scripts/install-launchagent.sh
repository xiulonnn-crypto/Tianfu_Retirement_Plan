#!/bin/sh
set -eu
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
agent_file="$HOME/Library/LaunchAgents/com.tianfu.retirement.daily.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|REPLACE_WITH_ABSOLUTE_PROJECT_PATH|$project_dir|g" \
  "$project_dir/scripts/com.tianfu.retirement.daily.plist.example" > "$agent_file"
plutil -lint "$agent_file"
launchctl bootout "gui/$(id -u)" "$agent_file" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$agent_file"
sleep 1
exit_code="$(launchctl print "gui/$(id -u)/com.tianfu.retirement.daily" | awk -F'= ' '/last exit code =/{print $2;exit}')"
if [ -n "$exit_code" ] && [ "$exit_code" != 0 ]; then
  echo "任务已安装，但首次运行失败（退出码 $exit_code）。查看 $project_dir/data/launch.err.log" >&2
  exit 1
fi
echo "已安装每日同步: $agent_file"
