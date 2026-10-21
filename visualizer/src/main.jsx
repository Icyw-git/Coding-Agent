import React, { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import {
  Activity, AlertTriangle, ArrowDown, Check, ChevronRight, CircleDot,
  Clock3, Code2, Cpu, Database, FileJson2, GitBranch, Layers3, ListTodo,
  Pause, Play, RotateCcw, Send, ServerCog, Settings2, Sparkles, Terminal,
  Users, Wrench, Zap
} from 'lucide-react'
import './styles.css'

const navItems = [
  { id: 'main', label: 'Main Loop', icon: Activity, count: '08' },
  { id: 'compression', label: 'Compression', icon: Layers3, count: '04' },
  { id: 'tools', label: 'Tool Runtime', icon: Wrench, count: '06' },
  { id: 'collaboration', label: 'Collaboration', icon: Users, count: '09' },
]

const phases = {
  main: {
    eyebrow: 'SESSION / OPENAI', title: '主循环如何推进一次请求',
    description: '从用户输入到下一次迭代，每一个消息、上下文和工具状态都可追踪。',
    source: 'harness.py:330',
    nodes: [
      { id: 'input', label: 'USER INPUT', kicker: 'message', icon: Send, detail: '用户请求写入 canonical history，并作为 session 的第一条 message 事件。', source: 'harness.py:330-336', color: 'cyan', json: { type: 'message', role: 'user', content: '检查测试状态并修复失败项' } },
      { id: 'context', label: 'CONTEXT PREP', kicker: 'update_context', icon: Database, detail: '收集 workspace、tools、skills 和 memory index，组装可缓存的 system prompt。', source: 'harness.py:337-348', color: 'violet', json: { type: 'context', enabled_tools: 24, memories: 5, skills: 1 } },
      { id: 'llm', label: 'LLM DECISION', kicker: 'chat.completions.create', icon: Cpu, detail: '请求副本前置 system prompt，并把相关记忆附加到第一条 user 消息。', source: 'harness.py:383-400', color: 'blue', json: { type: 'llm_call', model: 'deepseek-v4-flash', finish_reason: 'tool_calls' } },
      { id: 'tool', label: 'TOOL EXECUTION', kicker: 'exec_tool_call', icon: Wrench, detail: '工具经过 PreToolUse、权限判断、前台/后台分流和 PostToolUse，再回填 role=tool。', source: 'harness.py:285-315', color: 'amber', json: { type: 'tool_started', name: 'bash', tool_call_id: 'call_0042' } },
      { id: 'iterate', label: 'NEXT ITERATION', kicker: 'while True', icon: RotateCcw, detail: '工具结果加入历史，更新 context 和 system，进入下一轮压缩与 LLM 调用。', source: 'harness.py:477-495', color: 'green', json: { type: 'iteration', round: 2, pending_tools: 0 } },
    ]
  },
  compression: {
    eyebrow: 'CONTEXT / BUDGET', title: '上下文如何逐层收缩',
    description: '先使用零 API 成本的处理，仍然超限时才调用 LLM 摘要。', source: 'harness.py:267',
    nodes: [
      { id: 'budget', label: 'RESULT BUDGET', kicker: 'tool_result_budget', icon: FileJson2, detail: '大型工具输出落盘到 .cache/tool_results，只在消息内保留引用和预览。', source: 'agent_core.py:449-468', color: 'amber', json: { type: 'persist', path: '.cache/tool_results/call_0042.txt', preview_chars: 2000 } },
      { id: 'snip', label: 'SNIP HISTORY', kicker: 'snip_compact', icon: Layers3, detail: '超过 50 条消息时保留头尾，中间变成 snipped 标记，并保护 tool 配对。', source: 'agent_core.py:388-420', color: 'cyan', json: { type: 'snip', kept: 'head 3 + tail 47', removed: 17 } },
      { id: 'micro', label: 'MICRO COMPACT', kicker: 'micro_compact', icon: Zap, detail: '旧工具结果替换成占位符，只保留最近 KEEP_RECENT 条完整结果。', source: 'agent_core.py:427-435', color: 'violet', json: { type: 'micro_compact', kept_recent: 6, compacted: 8 } },
      { id: 'summary', label: 'LLM SUMMARY', kicker: 'compact_history', icon: Sparkles, detail: '转录完整历史，调用摘要模型，用 [Compacted] 记录替换旧消息。', source: 'agent_core.py:498-503', color: 'coral', json: { type: 'compaction', strategy: 'auto', summary: 'Goal, findings, files, remaining work, constraints' } },
    ]
  },
  tools: {
    eyebrow: 'RUNTIME / TOOLS', title: '一次工具调用经过哪些闸门',
    description: '工具不是直接执行：它要经过审计、权限、后台分流和结果回填。', source: 'harness.py:285',
    nodes: [
      { id: 'call', label: 'TOOL CALL', kicker: 'assistant.tool_calls', icon: Terminal, detail: '模型先产生带 id、name 和 arguments 的 OpenAI tool call。', source: 'harness.py:412-415', color: 'blue', json: { role: 'assistant', tool_calls: [{ id: 'call_0042', name: 'bash' }] } },
      { id: 'pre', label: 'PRE TOOL USE', kicker: 'permission_hook', icon: Settings2, detail: '检查工具是否注册、命令是否危险、路径是否越界；阻断也返回 tool error。', source: 'hooks.py:13-40', color: 'coral', json: { hook: 'PreToolUse', result: 'allowed' } },
      { id: 'route', label: 'ROUTE', kicker: 'foreground / background / MCP', icon: GitBranch, detail: '普通工具同步执行，慢 bash 进入后台，MCP 工具桥接到 asyncio loop。', source: 'harness.py:297-309', color: 'amber', json: { route: 'background', task_id: 'bg_0001', command: 'pytest -q' } },
      { id: 'post', label: 'POST TOOL USE', kicker: 'log_hook', icon: Check, detail: '记录工具输出，提示超大结果，然后生成 role=tool 消息。', source: 'hooks.py:42-49', color: 'green', json: { hook: 'PostToolUse', output_chars: 1842 } },
      { id: 'result', label: 'ROLE=TOOL', kicker: 'message append', icon: ArrowDown, detail: '结果和 tool_call_id 配对写入 session JSONL，下一轮发送给 LLM。', source: 'harness.py:469-472', color: 'cyan', json: { role: 'tool', tool_call_id: 'call_0042', content: 'Test output...' } },
      { id: 'mcp', label: 'MCP BRIDGE', kicker: 'optional path', icon: ServerCog, detail: 'MCP schema 动态转换成 OpenAI function；同步 wrapper 等待异步 server 结果。', source: 'mcp_bridge.py:76-95', color: 'violet', json: { server: 'filesystem', tool: 'read_file', transport: 'asyncio bridge' } },
    ]
  },
  collaboration: {
    eyebrow: 'TEAM / TASK BOARD', title: '任务如何在 Agent 之间流转',
    description: 'Lead、teammate、任务板、邮箱协议和 worktree 共同组成协作循环。', source: 'agent_teams.py:215',
    nodes: [
      { id: 'create', label: 'CREATE TASK', kicker: 'task_system', icon: ListTodo, detail: '任务写入 .cache/tasks，初始状态为 pending，可携带 blockedBy 依赖。', source: 'task_system.py:30-41', color: 'cyan', json: { type: 'task', id: 'task_02', status: 'pending', blockedBy: ['task_01'] } },
      { id: 'mail', label: 'MAILBOX', kicker: '.mailbox/<agent>.jsonl', icon: Send, detail: 'Lead 通过 JSONL mailbox 发送普通消息、shutdown 或 plan request。', source: 'agent_teams.py:29-65', color: 'violet', json: { type: 'plan_approval_request', to: 'alice', request_id: 'req_0012' } },
      { id: 'claim', label: 'CLAIM TASK', kicker: 'idle_poll', icon: CircleDot, detail: 'teammate 空闲时扫描第一个依赖满足且未认领的任务，切换 owner 和状态。', source: 'autonomous_agent.py:58-75', color: 'amber', json: { type: 'task_claim', owner: 'alice', status: 'in_progress' } },
      { id: 'worktree', label: 'WORKTREE', kicker: 'isolated cwd', icon: GitBranch, detail: '任务绑定的 git worktree 成为 teammate 的 cwd，修改与主工作区隔离。', source: 'agent_teams.py:193-211', color: 'green', json: { type: 'worktree', name: 'wt-tests', cwd: '.worktrees/wt-tests' } },
      { id: 'complete', label: 'COMPLETE', kicker: 'unblock dependents', icon: Check, detail: '任务完成后扫描依赖，报告被解锁任务，并清空 teammate 的 worktree 上下文。', source: 'task_system.py:67-76', color: 'green', json: { type: 'task_complete', id: 'task_02', unblocked: ['task_03'] } },
      { id: 'idle', label: 'IDLE POLL', kicker: '5s / 60s', icon: Clock3, detail: 'teammate 没有最终工作时进入空闲循环，继续收 inbox 或自动认领新任务。', source: 'autonomous_agent.py:29-78', color: 'blue', json: { type: 'idle_poll', timeout: 60, interval: 5 } },
    ]
  }
}

function App() {
  const [active, setActive] = useState('main')
  const [current, setCurrent] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [selected, setSelected] = useState(0)
  const phase = phases[active]
  const node = phase.nodes[selected]
  const visibleNodes = phase.nodes.slice(0, current + 1)
  const eventRows = useMemo(() => phase.nodes.slice(0, current + 1).map((n, index) => ({ ...n, seq: index + 12 })), [phase, current])

  useEffect(() => {
    if (!playing) return undefined
    if (current >= phase.nodes.length - 1) { setPlaying(false); return undefined }
    const timer = setTimeout(() => setCurrent((v) => v + 1), 1200 / speed)
    return () => clearTimeout(timer)
  }, [playing, current, phase.nodes.length, speed])

  useEffect(() => { setCurrent(0); setSelected(0); setPlaying(false) }, [active])

  const step = () => setCurrent((v) => Math.min(v + 1, phase.nodes.length - 1))
  const reset = () => { setCurrent(0); setSelected(0); setPlaying(false) }

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark"><Activity size={17} /></div><div><div className="brand-name">HARNESS LOOP</div><div className="brand-sub">OBSERVATORY / RUNTIME TRACE</div></div></div>
      <div className="session-chip"><span className="live-dot" /> SESSION_20260820_001 <span className="chip-divider" /> OPENAI</div>
      <div className="top-actions"><button className="icon-btn" title="Settings"><Settings2 size={16} /></button><span className="status-label">LOCAL DEMO <span className="status-dot" /></span></div>
    </header>
    <div className="workspace">
      <aside className="sidebar">
        <div className="side-label">FLOW CATALOG</div>
        <nav>{navItems.map(({ id, label, icon: Icon, count }) => <button key={id} className={`nav-item ${active === id ? 'active' : ''}`} onClick={() => setActive(id)}><Icon size={16} /><span>{label}</span><b>{count}</b></button>)}</nav>
        <div className="sidebar-rule" />
        <div className="side-label">SESSION STATE</div>
        <div className="state-stack"><StateRow label="context" value="42.8k / 65k" tone="cyan" /><StateRow label="round" value={`${current + 1} / ${phase.nodes.length}`} tone="violet" /><StateRow label="background" value="1 running" tone="amber" /><StateRow label="tasks" value="2 pending" tone="green" /></div>
        <div className="sidebar-foot"><div className="foot-line"><span className="pulse-line" /> Event stream connected</div><div className="foot-file">.cache/sessions/session_...jsonl</div></div>
      </aside>
      <main className="main-panel">
        <div className="main-head"><div><div className="eyebrow"><span className="eyebrow-line" /> {phase.eyebrow}</div><h1>{phase.title}</h1><p>{phase.description}</p></div><div className="source-ref"><Code2 size={14} /> {phase.source}</div></div>
        <div className="flow-toolbar"><div className="flow-title"><span className="tiny-square" /> LIVE TRACE <span className="slash">/</span> {active.toUpperCase()}</div><div className="playback"><button className="tool-btn primary" onClick={() => setPlaying((v) => !v)} title={playing ? 'Pause' : 'Play'}>{playing ? <Pause size={15} /> : <Play size={15} />}{playing ? 'Pause' : 'Play'}</button><button className="tool-btn" onClick={step} title="Step"><ChevronRight size={15} /> Step</button><button className="tool-btn" onClick={reset} title="Reset"><RotateCcw size={14} /> Reset</button><select value={speed} onChange={(e) => setSpeed(Number(e.target.value))} aria-label="Playback speed"><option value="0.5">0.5x</option><option value="1">1x</option><option value="2">2x</option></select></div></div>
        <section className="flow-canvas"><div className="canvas-grid" /> <div className="flow-track">{phase.nodes.map((item, index) => <React.Fragment key={item.id}><FlowNode item={item} index={index} current={current} selected={selected} onClick={() => setSelected(index)} />{index < phase.nodes.length - 1 && <Connector active={index < current} />}</React.Fragment>)}</div><div className="canvas-caption"><span>PLAYBACK POSITION</span><div className="progress-track"><div style={{ width: `${((current + 1) / phase.nodes.length) * 100}%` }} /></div><strong>{String(current + 1).padStart(2, '0')} / {String(phase.nodes.length).padStart(2, '0')}</strong></div></section>
        <section className="trace-strip"><div className="strip-heading"><div><span className="eyebrow-line" /> EVENT STREAM</div><span>append-only / JSONL</span></div><div className="events">{eventRows.map((event) => <button key={event.id} className={`event-row ${selected === phase.nodes.indexOf(event) ? 'selected' : ''}`} onClick={() => setSelected(phase.nodes.indexOf(event))}><span className="event-seq">{event.seq}</span><span className={`event-bullet ${event.color}`} /><span className="event-name">{event.kicker}</span><span className="event-detail">{event.label}</span><span className="event-time">00:{String((event.seq - 10) * 7).padStart(2, '0')}</span></button>)}</div></section>
      </main>
      <aside className="inspector"><div className="inspector-head"><div><div className="side-label">SELECTED EVENT</div><h2>{node.label}</h2></div><span className={`inspector-status ${current >= selected ? 'done' : ''}`}>{current >= selected ? 'COMPLETED' : 'PENDING'}</span></div><div className="inspector-kicker"><span className={`event-bullet ${node.color}`} /> {node.kicker}</div><p className="inspector-detail">{node.detail}</p><div className="source-card"><div className="source-label"><Code2 size={13} /> SOURCE</div><code>{node.source}</code></div><div className="payload-label"><FileJson2 size={13} /> EVENT PAYLOAD</div><pre>{JSON.stringify(node.json, null, 2)}</pre><div className="inspector-note"><Zap size={14} /><span>Click another node or use Step to follow the next state transition.</span></div></aside>
    </div>
    <footer className="footerbar"><div><span className="footer-key">RUNTIME</span> harness.py <span className="muted">·</span> agent_core.py <span className="muted">·</span> session_store.py</div><div className="footer-right"><span><span className="legend cyan" /> normal</span><span><span className="legend amber" /> tool</span><span><span className="legend violet" /> context</span><span><span className="legend coral" /> recovery</span></div></footer>
  </div>
}

function StateRow({ label, value, tone }) { return <div className="state-row"><span><i className={`state-dot ${tone}`} /> {label}</span><b>{value}</b></div> }
function Connector({ active }) { return <div className={`connector ${active ? 'active' : ''}`}><span /></div> }
function FlowNode({ item, index, current, selected, onClick }) { const Icon = item.icon; const status = index < current ? 'complete' : index === current ? 'current' : 'pending'; return <button onClick={onClick} className={`flow-node ${item.color} ${status} ${selected === index ? 'selected' : ''}`}><div className="node-top"><span className="node-index">0{index + 1}</span><span className="node-state">{status === 'complete' ? <Check size={12} /> : status === 'current' ? <span className="node-pulse" /> : <CircleDot size={12} />}</span></div><div className="node-icon"><Icon size={19} /></div><div className="node-label">{item.label}</div><div className="node-kicker">{item.kicker}</div></button> }

createRoot(document.getElementById('root')).render(<App />)
