====================================
  ParkTerm V2.0.0 源代码包
====================================

目录结构
--------
  app.py                  -- 入口文件（仅调用 parkterm.main.main()）
  app.ico                 -- EXE 图标文件
  build.bat               -- Windows 一键构建脚本
  ParkTerm_V2.0.0.spec   -- PyInstaller 构建配置
  requirements.txt        -- Python 依赖声明
  parkterm/               -- 核心业务包（模块化重构）
    __init__.py           -- 包入口，导出版本号
    config.py             -- 集中化配置（版本/超时/路径/端口/设备命令）
    legacy_ssh.py         -- Legacy SSH 算法恢复 + raw socket 回退
    ssh_session.py        -- SSH 会话管理（连接/流/命令/二级权限/设备信息）
    file_manager.py       -- 文件操作三级降级（SFTP→SCP→base64）
    capture.py            -- 抓包管理（按会话隔离的状态机）
    server.py             -- Flask + SocketIO 路由 + 会话存储
    main.py               -- 入口逻辑（日志/端口探测/启动动画/服务启动）
  templates/
    index.html            -- 前端单页模板（依赖已本地化，无需联网）
  static/                 -- 前端静态资源（随 EXE 打包，离线可用）
    css/xterm.min.css
    js/xterm.min.js
    js/addon-fit.min.js
    js/addon-web-links.min.js
    js/socket.io.min.js
  tests/                  -- pytest 单元测试
    conftest.py
    test_config.py
    test_file_manager.py
    test_capture.py
    test_ssh_session.py
  mock_device.py          -- 本地模拟 SSH 设备（用于集成测试）

环境要求
--------
- Python 3.10+
- pip 已安装
- 无需联网（前端依赖已全部本地化打包）

方式一：直接运行（调试模式）
--------
1. 安装依赖：
   pip install -r requirements.txt
2. 启动服务：
   python app.py
3. 浏览器将自动打开 http://127.0.0.1:<port>

方式二：构建 EXE（发布模式）
--------
双击 build.bat 即可，输出文件位于 dist/ 目录。

运行单元测试
--------
  pip install pytest
  pytest tests/ -v

V2.0.0 主要改进
--------
1. 架构模块化：原 82KB 单文件 app.py 拆分为 8 个职责清晰的模块
2. 前端依赖本地化：xterm.js / socket.io 全部打包到 static/，离线环境可用
3. 配置集中化：所有超时/端口/路径/版本号统一在 config.py，发版改一处
4. 抓包状态按会话隔离：消除原全局字典在多会话场景下的数据污染
5. 命令注入修复：所有 shell 命令拼接统一使用 shlex.quote()，addr/port 做白名单校验
6. SSH keepalive：启用 transport.set_keepalive(30)，防止长时间空闲被断开
7. 阻塞 recv 替代轮询：流式输出使用 settimeout + 阻塞 recv，降低 CPU 空转
8. 会话定时清理：后台线程每 60s 清理过期会话，避免异常退出的连接残留
9. CORS 限制：仅允许 127.0.0.1 / localhost 来源，防止恶意网页跨站请求
10. 单元测试：新增 pytest 测试覆盖 ls 解析、抓包过滤、ANSI 清理、配置验证
11. 设备信息解析数据驱动：命令列表和字段正则统一在 config.py，便于扩展新设备

技术栈
--------
- Flask >= 3.0            Web 后端框架
- Flask-SocketIO >= 5.3   实时双向通信（threading 模式）
- Paramiko >= 3.4,<4      SSH/SFTP 客户端（保留 ssh-rsa 兼容）
- scp >= 0.14              SCP 协议回退
- PyInstaller >= 6.0       EXE 打包工具
- xterm.js 5.5.0           终端仿真器（本地打包）
- Socket.IO 4.7.5          前端 WebSocket 客户端（本地打包）

功能说明
--------
1. 双击 EXE 或 python app.py 启动
2. 浏览器自动打开登录页面
3. 填写服务器 IP、端口（默认 23333）、用户名、密码
4. 点击"连接"进入一级终端
5. 顶栏自动加载设备型号/版本/SN 信息
6. 一级环境自动检测：执行 ls，若输出"Not Support Cmd Yet!"则显示"进入二级"按钮
7. 点击"进入二级"输入二级密码完成权限提升
8. 文件管理器解锁，可上传/下载/浏览/删除文件（SFTP→SCP→base64 三级降级）
9. 抓包管理：基于 tcpdump 的远程抓包，支持保存/启动/结束/删除/下载
10. 工具栏常用功能：服务查看、内存查看、磁盘空间、网口信息

端口说明：
- 1.3.7.P02 以前版本：SSH 端口 22
- 1.3.7.P02 及以后版本：SSH 端口 23333

日志文件：
- parkterm.log 位于 EXE 同目录，记录所有 SSH 连接过程
