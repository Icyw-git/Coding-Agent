import json
import threading

_bg_counter=0
background_tasks:dict[str,dict]={}
background_results:dict[str,str]={}
background_lock=threading.Lock() # 保护 background_tasks 和 background_results 访问

def is_slow_operation(tool_name:str,tool_input:dict)->bool:
    if tool_name!='bash':
        return False
    cmd=tool_input.get('command','').lower()
    slow_keywords=["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords) # 检查命令是否包含慢操作关键词，判断是否为慢操作。

# 检查是否应该在后台运行任务
def should_run_background(tool_name:str,tool_input:dict)->bool:
    
    if tool_input.get('run_in_background'):
        return True
    return is_slow_operation(tool_name,tool_input)

def execute_tool(tool_name:str,tool_input:dict,registry:dict)->str: #执行工具，返回结果
    handler=registry.get(tool_name)
    if handler:
        return handler(**tool_input)
    return f'Unknown tool: {tool_name}'

def start_background_task(tool_call,registry:dict)->str: #启动后台任务，返回任务 ID
    global _bg_counter
    _bg_counter+=1
    bg_id=f'bg_{_bg_counter:04d}'
    name=tool_call.function.name
    args=json.loads(tool_call.function.arguments)
    cmd=args.get('command','')

    def worker(): #后台任务线程函数
        result=execute_tool(name,args,registry)
        with background_lock:
            background_tasks[bg_id]['status']='completed'
            background_results[bg_id]=result

    with background_lock: # 加锁保护 background_tasks 和 background_results 访问，注册任务 ID
        background_tasks[bg_id]={
            'tool_use_id':tool_call.id,
            'status':'running',
            'command':cmd,
        }
    thread=threading.Thread(target=worker,daemon=True) # 启动后台任务线程
    thread.start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def collect_background_results()->list[dict]: #收集后台任务结果，返回通知列表
   
    with background_lock: # 加锁保护 background_tasks 和 background_results 访问
        ready_ids=[bid for bid,task in background_tasks.items() if task['status']=='completed']

    notifications=[]
    for bg_id in ready_ids:
        with background_lock:
            task=background_tasks.pop(bg_id)
            output=background_results.pop(bg_id,'')
        summary=output[:200] if len(output)>200 else output
        notifications.append(
            f'<task_notification>\n'
            f' <task_id>{bg_id}</task_id>\n'
            f'<status>completed</status>\n'
            f'<command>{task["command"]}</command>\n'
            f'<summary>{summary}</summary>\n'
            f'</task_notification>'
        )
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications
