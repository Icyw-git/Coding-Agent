import os 
from dotenv import load_dotenv
import subprocess
import openai
import json



load_dotenv()
client=openai.OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL"),
    
)

SYSTEM=f'You are a coding agent at {os.getcwd()}, use bash to execute shell commands'

TOOLS=[{
    "type":"function",
    "function":{
        "name":"bash",
        "description":"Execute shell commands",
        "parameters":{
            "type":"object",
            "properties":{
                "command":{
                    "type":"string",
                    "description":"The shell command to execute, e.g. 'ls'"
                }
            },
            "required":[
                "command"
            ]

        }
    }

}]

def run_bash(command:str)->str:

    dangerous=["rm -rf /","sudo","shutdown","reboot","> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command, please do not execute"
    try:
        r=subprocess.run(command,shell=True,cwd=os.getcwd(),capture_output=True,text=True,timeout=120)
        out=(r.stdout+r.stderr).strip()
        return out[:50000] if out else "No output"
    except subprocess.TimeoutExpired:
        return "Error: Command timeout(120s)"
    except (FileNotFoundError,OSError) as e:
        return f'Error:{e}'

def agent_loop(messages:list):
    messages.append({'role':'system','content':SYSTEM})

    while True:
        response=client.chat.completions.create(

            model=os.getenv("LLM_MODEL_ID"),
            messages=messages,
            tools=TOOLS,
            temperature=0.7,
            max_tokens=8000,
        )

        message=response.choices[0].message
        messages.append(message.model_dump())

        if response.choices[0].finish_reason != "tool_calls":
            if message.content:
                print(message.content)
            return

        tool_messages = []
        for tool_call in message.tool_calls:
            if tool_call.function.name == "bash":
                args = json.loads(tool_call.function.arguments)
                command = args["command"]
                print(f'\033[33m$ {command}\033[0m')
                output = run_bash(command)
                print(output[:200])
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output,
                })

        messages.extend(tool_messages)

if __name__ == '__main__':
    messages = [{'role': 'user', 'content': '帮我删除文件test.txt'}]
    agent_loop(messages)