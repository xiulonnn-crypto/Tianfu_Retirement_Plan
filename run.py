"""One-command local launcher, following the reference project's run.py flow."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import webbrowser


ROOT=Path(__file__).resolve().parent
REQUIRED=('flask','requests','yfinance','numpy','scipy','pandas')


def main():
    missing=[name for name in REQUIRED if importlib.util.find_spec(name) is None]
    if missing:
        print('正在安装项目依赖：'+', '.join(missing),flush=True)
        subprocess.check_call([sys.executable,'-m','pip','install','-r',str(ROOT/'requirements.txt')],cwd=str(ROOT))
    env=os.environ.copy()
    env.setdefault('PYTHONPYCACHEPREFIX',str(Path(tempfile.gettempdir())/'tianfu-pycache'))
    port=int(env.get('PORT','10020'))
    if port<1024 and hasattr(os,'geteuid') and os.geteuid()!=0:
        raise SystemExit(f'端口 {port} 需要管理员权限；请使用默认 10020 或设置 PORT 为 1024 以上。')
    url=f'http://localhost:{port}/#overview'
    print(f'启动模拟交易系统：{url}',flush=True)
    process=subprocess.Popen([sys.executable,'-u',str(ROOT/'server.py')],cwd=str(ROOT),env=env)
    try:
        time.sleep(1)
        if process.poll() is not None:
            raise SystemExit(f'服务启动失败，退出码 {process.returncode}')
        webbrowser.open(url)
        print('已打开浏览器。按 Ctrl+C 停止本次启动的服务。',flush=True)
        process.wait()
    except KeyboardInterrupt:
        print('\n正在停止服务…',flush=True)
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()


if __name__=='__main__':main()
