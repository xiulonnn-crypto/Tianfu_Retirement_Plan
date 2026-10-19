#!/bin/sh
# Launch the locally installed Theta Terminal with a key kept out of source control.
set -eu

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
theta_dir="$project_dir/runtime/theta"
java_home="$(/usr/libexec/java_home -v 21 2>/dev/null || true)"
java_bin="${java_home:+$java_home/bin/java}"
if [ -z "$java_bin" ] || [ ! -x "$java_bin" ]; then
  java_bin="$project_dir/runtime/jdk/Contents/Home/bin/java"
fi
jar="$theta_dir/ThetaTerminalv3.jar"
env_file="$theta_dir/.env"

if [ ! -x "$java_bin" ]; then
  echo "找不到 Java 21：$java_bin" >&2
  exit 1
fi
if [ ! -f "$jar" ]; then
  echo "找不到 Theta Terminal：$jar" >&2
  exit 1
fi

if [ -f "$env_file" ]; then
  set -a
  # This file is local-only and must contain a ThetaData API key.
  . "$env_file"
  set +a
fi
if [ -z "${THETADATA_API_KEY:-}" ]; then
  echo "缺少 THETADATA_API_KEY。请把密钥写入 $env_file 后重试。" >&2
  exit 2
fi

exec "$java_bin" -jar "$jar"
