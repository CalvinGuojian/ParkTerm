import paramiko
import time

host = '172.1.0.50'
port = 23333
username = 'root'
password = 'admin_123'
level2_username = 'zhimakaimen'
level2_password = '7]34340W'

print('=== SSH Interactive Shell + Level2 (ParkTerm Way) ===')
try:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, 
                   timeout=15, look_for_keys=False, allow_agent=False)
    print('SSH connected!')

    transport = client.get_transport()
    channel = transport.open_session()
    channel.get_pty()
    channel.invoke_shell()
    
    # 等待初始提示
    time.sleep(2)
    output = channel.recv(65536).decode('utf-8', errors='replace')
    print('=== Initial ===')
    print(output[:300])
    
    # ParkTerm 进入二级的方式
    print('\n=== Entering Level2 (ParkTerm Way) ===')
    
    channel.send("login\n")
    time.sleep(1.5)
    
    channel.send(level2_username)  # zhimakaimen
    time.sleep(0.1)
    
    # Ctrl+Q and Ctrl+W
    channel.send("\x11")
    time.sleep(0.1)
    channel.send("\x17")
    time.sleep(0.1)
    
    channel.send("\n")
    time.sleep(1.5)
    
    # 发送二级密码
    channel.send(level2_password + "\n")
    time.sleep(3)
    
    output = channel.recv(65536).decode('utf-8', errors='replace')
    print('=== After Level2 Password ===')
    print(output)
    
    # 检查是否成功进入二级
    if "Not Support" not in output and "Incorrect" not in output and "locked" not in output.lower():
        print('\n=== Level2 Entered Successfully! ===')
        
        # 执行ls命令测试
        print('\n=== Send: ls /root ===')
        channel.send('ls /root\n')
        time.sleep(3)
        output = channel.recv(65536).decode('utf-8', errors='replace')
        print(output)
        
        # 检查抓包目录
        print('\n=== Send: ls -la /root/parkterm_captures/ ===')
        channel.send('ls -la /root/parkterm_captures/\n')
        time.sleep(3)
        output = channel.recv(65536).decode('utf-8', errors='replace')
        print(output)
        
        # 检查tcpdump
        print('\n=== Send: which tcpdump ===')
        channel.send('which tcpdump\n')
        time.sleep(3)
        output = channel.recv(65536).decode('utf-8', errors='replace')
        print(output)
    else:
        print('\n=== Level2 Entry Failed ===')
    
    channel.close()
    transport.close()
    client.close()
except Exception as e:
    print(f'Error: {type(e).__name__}: {e}')
    import traceback
    traceback.print_exc()
