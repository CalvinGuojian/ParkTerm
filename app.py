"""
ParkTerm - SSH Terminal Manager
入口文件：仅负责调用模块化的 main()。

所有业务逻辑已拆分到 parkterm/ 包中：
  - config.py        集中化配置
  - legacy_ssh.py    Legacy SSH 兼容 + raw socket 回退
  - ssh_session.py   SSH 会话管理（连接/流/命令/二级权限/设备信息）
  - file_manager.py  文件操作三级降级（SFTP→SCP→base64）
  - capture.py       抓包管理（按会话隔离）
  - server.py        Flask + SocketIO 路由
  - main.py          入口逻辑（日志/端口/动画/启动）
"""
from parkterm.main import main

if __name__ == "__main__":
    main()
