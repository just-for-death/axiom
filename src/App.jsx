import { useState, useEffect, useRef, useCallback, useMemo } from "react";

// ─────────────────────────────────────────────────────────────────────────────
// AXIOM — Log Server UI
// Aesthetic: Warm amber terminal  |  Space Mono + Syne  |  Carbon-dark base
// ─────────────────────────────────────────────────────────────────────────────

const API = "/api";

const T = {
  // Base
  bg0:      "#0c0a08",
  bg1:      "#111009",
  bg2:      "#17140f",
  bg3:      "#1e1c16",
  border:   "#2a2720",
  border2:  "#3d3a30",
  // Text
  tx0:      "#e8dfc8",   // bright text
  tx1:      "#a09070",   // dim text
  tx2:      "#5a5040",   // faint text
  // Accents
  amber:    "#f59e0b",
  amberDim: "#92600a",
  amberFaint:"#3d2e08",
  // Severity
  crit:     "#f87171",
  critBg:   "rgba(248,113,113,0.07)",
  err:      "#fb923c",
  errBg:    "rgba(251,146,60,0.06)",
  warn:     "#fbbf24",
  warnBg:   "rgba(251,191,36,0.04)",
  good:     "#34d399",
  // AI
  ai:       "#818cf8",
  aiDim:    "#3730a3",
  aiBg:     "rgba(129,140,248,0.06)",
};

const SOURCES = [
  { id:"syslog", label:"System",   icon:SysIcon,    color:T.amber,  desc:"Daemons · kernel events" },
  { id:"kernel", label:"Kernel",   icon:KrnIcon,    color:T.warn,   desc:"Hardware · drivers" },
  { id:"auth",   label:"Auth",     icon:AuthIcon,   color:T.good,   desc:"SSH · sudo · logins" },
  { id:"docker", label:"Docker",   icon:DockerIcon, color:T.err,    desc:"Container logs" },
  { id:"disk",   label:"Disk",     icon:DiskIcon,   color:T.ai,     desc:"Storage · I/O" },
  { id:"boot",   label:"Boot",     icon:BootIcon,   color:"#94a3b8", desc:"Boot · dmesg" },
  { id:"smart",  label:"S.M.A.R.T",icon:SmartIcon,  color:T.good,   desc:"Drive health" },
  { id:"_sysmon",label:"Sysmon",   icon:AppIcon,    color:T.tx1,    desc:"App activity", divider:true },
];

// ─────────────────────────────────────────────────────────────────────────────
// SVG ICON LIBRARY
// ─────────────────────────────────────────────────────────────────────────────
function Ico({ sz=16, children, style }) {
  return <svg width={sz} height={sz} viewBox="0 0 16 16" fill="none" style={style}>{children}</svg>;
}
function SysIcon({ sz=16, col })   { return <Ico sz={sz}><circle cx="8" cy="8" r="5.5" stroke={col} strokeWidth="1.4"/><circle cx="8" cy="8" r="2" fill={col}/><line x1="8" y1="2" x2="8" y2="4" stroke={col} strokeWidth="1.4" strokeLinecap="round"/><line x1="8" y1="12" x2="8" y2="14" stroke={col} strokeWidth="1.4" strokeLinecap="round"/><line x1="2" y1="8" x2="4" y2="8" stroke={col} strokeWidth="1.4" strokeLinecap="round"/><line x1="12" y1="8" x2="14" y2="8" stroke={col} strokeWidth="1.4" strokeLinecap="round"/></Ico>; }
function KrnIcon({ sz=16, col })   { return <Ico sz={sz}><rect x="2" y="4" width="12" height="8" rx="1.5" stroke={col} strokeWidth="1.4"/><line x1="5" y1="7" x2="5" y2="9" stroke={col} strokeWidth="1.4" strokeLinecap="round"/><line x1="8" y1="6" x2="8" y2="10" stroke={col} strokeWidth="1.4" strokeLinecap="round"/><line x1="11" y1="7" x2="11" y2="9" stroke={col} strokeWidth="1.4" strokeLinecap="round"/></Ico>; }
function AuthIcon({ sz=16, col })  { return <Ico sz={sz}><rect x="4" y="7" width="8" height="6" rx="1.5" stroke={col} strokeWidth="1.4"/><path d="M5.5 7V5.5a2.5 2.5 0 0 1 5 0V7" stroke={col} strokeWidth="1.4" strokeLinecap="round"/><circle cx="8" cy="10" r="1" fill={col}/></Ico>; }
function DockerIcon({ sz=16, col }){ return <Ico sz={sz}><rect x="1" y="6" width="4" height="3" rx=".7" stroke={col} strokeWidth="1.3"/><rect x="6" y="6" width="4" height="3" rx=".7" stroke={col} strokeWidth="1.3"/><rect x="11" y="6" width="4" height="3" rx=".7" stroke={col} strokeWidth="1.3"/><rect x="6" y="2" width="4" height="3" rx=".7" stroke={col} strokeWidth="1.3"/><path d="M2 9c0 3 2 4 6 4s6-1 6-4" stroke={col} strokeWidth="1.3"/></Ico>; }
function DiskIcon({ sz=16, col })  { return <Ico sz={sz}><ellipse cx="8" cy="5" rx="5.5" ry="2.5" stroke={col} strokeWidth="1.4"/><path d="M2.5 5v6c0 1.38 2.46 2.5 5.5 2.5s5.5-1.12 5.5-2.5V5" stroke={col} strokeWidth="1.4"/><line x1="2.5" y1="9" x2="13.5" y2="9" stroke={col} strokeWidth="1.2" opacity=".5"/></Ico>; }
function BootIcon({ sz=16, col })  { return <Ico sz={sz}><polyline points="3,12 8,4 13,12" stroke={col} strokeWidth="1.5" strokeLinejoin="round"/><line x1="5.5" y1="9" x2="10.5" y2="9" stroke={col} strokeWidth="1.5" strokeLinecap="round"/></Ico>; }
function SmartIcon({ sz=16, col }) { return <Ico sz={sz}><rect x="2" y="3" width="12" height="10" rx="2" stroke={col} strokeWidth="1.4"/><circle cx="8" cy="8" r="2.5" stroke={col} strokeWidth="1.3"/><line x1="10" y1="6" x2="12.5" y2="3.5" stroke={col} strokeWidth="1.3" strokeLinecap="round"/></Ico>; }
function AppIcon({ sz=16, col })   { return <Ico sz={sz}><rect x="2" y="2" width="12" height="12" rx="2" stroke={col} strokeWidth="1.4"/><line x1="5" y1="6"  x2="11" y2="6"  stroke={col} strokeWidth="1.3" strokeLinecap="round"/><line x1="5" y1="8.5" x2="9"  y2="8.5" stroke={col} strokeWidth="1.3" strokeLinecap="round"/><line x1="5" y1="11" x2="10" y2="11" stroke={col} strokeWidth="1.3" strokeLinecap="round"/></Ico>; }
function AiIcon({ sz=16, col })    { return <Ico sz={sz}><circle cx="8" cy="8" r="5.5" stroke={col} strokeWidth="1.4"/><path d="M5.5 9.5c.7 1 1.5 1.5 2.5 1.5s1.8-.5 2.5-1.5" stroke={col} strokeWidth="1.3" strokeLinecap="round"/><circle cx="6" cy="7" r="1" fill={col}/><circle cx="10" cy="7" r="1" fill={col}/></Ico>; }
function ChatIcon({ sz=16, col })  { return <Ico sz={sz}><path d="M2 3h12a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H5l-3 2V4a1 1 0 0 1 1-1z" stroke={col} strokeWidth="1.4"/><line x1="5" y1="7"  x2="11" y2="7"  stroke={col} strokeWidth="1.3" strokeLinecap="round"/><line x1="5" y1="9.5" x2="9"  y2="9.5" stroke={col} strokeWidth="1.3" strokeLinecap="round"/></Ico>; }
function SearchIcon({ sz=16, col }){ return <Ico sz={sz}><circle cx="6.5" cy="6.5" r="4" stroke={col} strokeWidth="1.5"/><line x1="9.5" y1="9.5" x2="14" y2="14" stroke={col} strokeWidth="1.5" strokeLinecap="round"/></Ico>; }
function RefreshIcon({ sz=16, col }){ return <Ico sz={sz}><path d="M13 6A5.5 5.5 0 1 0 11 11" stroke={col} strokeWidth="1.5" strokeLinecap="round"/><polyline points="13,2 13,6 9,6" stroke={col} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round"/></Ico>; }
function LiveIcon({ sz=16, col })  { return <Ico sz={sz}><circle cx="8" cy="8" r="3" fill={col}/><circle cx="8" cy="8" r="5.5" stroke={col} strokeWidth="1" opacity=".4"/><circle cx="8" cy="8" r="7.5" stroke={col} strokeWidth="0.7" opacity=".15"/></Ico>; }
function CloseIcon({ sz=16, col }) { return <Ico sz={sz}><line x1="4" y1="4" x2="12" y2="12" stroke={col} strokeWidth="1.8" strokeLinecap="round"/><line x1="12" y1="4" x2="4" y2="12" stroke={col} strokeWidth="1.8" strokeLinecap="round"/></Ico>; }
function SendIcon({ sz=16, col })  { return <Ico sz={sz}><line x1="14" y1="2" x2="7" y2="9" stroke={col} strokeWidth="1.6" strokeLinecap="round"/><polyline points="14,2 9,14 7,9 2,7 14,2" stroke={col} strokeWidth="1.6" strokeLinejoin="round"/></Ico>; }
function SeverityBar({ stats }) {
  const total = Math.max(stats.critical + stats.error + stats.warn, 1);
  return (
    <div style={{display:"flex",alignItems:"center",gap:4,height:16}}>
      {[["crit",T.crit,stats.critical],["err",T.err,stats.error],["warn",T.warn,stats.warn]].map(([k,c,n])=>
        n > 0 ? <div key={k} title={`${n} ${k}`} style={{height:10,width:Math.max(6,n/total*60),background:c,borderRadius:2,opacity:.85}}/> : null
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// LOG CLASSIFICATION
// ─────────────────────────────────────────────────────────────────────────────
function classify(line) {
  const l = line.toLowerCase();
  if (/\b(critical|panic|fatal|emergency|oom.kill|segfault|kernel.bug)\b/.test(l)) return "critical";
  if (/\b(error|err\b|failed|failure|denied|refused|timeout|abort|corrupt|cannot)\b/.test(l)) return "error";
  if (/\b(warn|warning|deprecated|retrying|slow|delay|high\s+load)\b/.test(l)) return "warn";
  if (/\b(success|started|enabled|connected|accepted|complete|ok\b|running)\b/.test(l)) return "good";
  return "normal";
}
const L = {
  critical: { color:T.crit, bg:T.critBg, bar:T.crit },
  error:    { color:T.err,  bg:T.errBg,  bar:T.err  },
  warn:     { color:T.warn, bg:T.warnBg, bar:T.warn },
  good:     { color:T.good, bg:"transparent", bar:"transparent" },
  normal:   { color:T.tx0,  bg:"transparent", bar:"transparent" },
};

// ─────────────────────────────────────────────────────────────────────────────
// HOOKS
// ─────────────────────────────────────────────────────────────────────────────
function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => { const id = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(id); }, []);
  return now;
}

// ─────────────────────────────────────────────────────────────────────────────
// SMART PANEL
// ─────────────────────────────────────────────────────────────────────────────
function SmartPanel({ data }) {
  const [open, setOpen] = useState({});
  if (!data) return null;
  const { drives=[], unreadable=[] } = data;
  return (
    <div style={{padding:"12px 16px",display:"flex",flexDirection:"column",gap:8}}>
      {drives.length===0 && unreadable.length===0 &&
        <div style={{padding:48,textAlign:"center",color:T.tx2,fontFamily:"'Space Mono',monospace",fontSize:11}}>No drives detected</div>
      }
      {drives.map((d,i) => {
        const ok=/passed/i.test(d.health), col=ok?T.good:T.crit;
        return (
          <div key={d.drive} style={{border:`1px solid ${col}28`,borderRadius:8,overflow:"hidden",background:T.bg1}}>
            <button onClick={()=>setOpen(o=>({...o,[d.drive]:!o[d.drive]}))}
              style={{display:"flex",alignItems:"center",gap:12,padding:"10px 14px",width:"100%",background:"transparent",border:"none",cursor:"pointer",textAlign:"left"}}>
              <div style={{width:8,height:8,borderRadius:"50%",background:col,boxShadow:`0 0 10px ${col}88`,flexShrink:0}}/>
              <span style={{flex:1,fontFamily:"'Space Mono',monospace",fontSize:11,color:T.tx0}}>{d.drive}</span>
              <span style={{fontFamily:"'Space Mono',monospace",fontSize:10,color:col}}>{d.health.replace(/.*SMART overall-health self-assessment test result:\s*/i,"")}</span>
              <span style={{color:T.tx2,fontSize:11}}>{open[d.drive]?"▲":"▼"}</span>
            </button>
            {open[d.drive]&&d.raw&&<pre style={{margin:0,padding:"10px 14px",fontFamily:"'Space Mono',monospace",fontSize:9,color:T.tx1,background:T.bg0,maxHeight:200,overflowY:"auto",whiteSpace:"pre-wrap",lineHeight:1.7}}>{d.raw}</pre>}
          </div>
        );
      })}
      {unreadable.length > 0 && (
        <div style={{marginTop:4}}>
          <div style={{fontSize:9,color:T.tx2,letterSpacing:2,padding:"4px 2px 6px",fontFamily:"'Space Mono',monospace"}}>
            UNSUPPORTED / VIRTUAL ({unreadable.length})
          </div>
          {unreadable.map((d,i) => (
            <div key={i} style={{display:"flex",alignItems:"center",gap:10,padding:"7px 14px",
              border:`1px solid ${T.border}`,borderRadius:8,background:T.bg1,marginBottom:4}}>
              <div style={{width:8,height:8,borderRadius:"50%",background:T.tx2,flexShrink:0}}/>
              <span style={{flex:1,fontFamily:"'Space Mono',monospace",fontSize:11,color:T.tx2}}>{d.drive}</span>
              <span style={{fontFamily:"'Space Mono',monospace",fontSize:10,color:T.tx2}}>{d.health}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// AI ANALYSIS PANEL  (auto-analyze mode)
// ─────────────────────────────────────────────────────────────────────────────
function AnalysisPanel({ sourceId, onClose }) {
  const [text, setText] = useState("");
  const [done, setDone] = useState(false);
  const [err,  setErr]  = useState("");
  useEffect(() => {
    setText(""); setDone(false); setErr("");
    const es = new EventSource(`${API}/analyze/${sourceId}?lines=120`);
    let buf = "";
    es.onmessage = e => {
      if (e.data==="[DONE]") { setDone(true); es.close(); return; }
      buf += e.data; setText(buf);
    };
    es.onerror = () => { setErr("Ollama not reachable — is it running on the host?"); setDone(true); es.close(); };
    return () => es.close();
  }, [sourceId]);

  return (
    <Panel color={T.amber} label="AI ANALYSIS" onClose={onClose} icon={<AiIcon sz={14} col={T.amber}/>}>
      {err
        ? <p style={{color:T.err,fontFamily:"'Space Mono',monospace",fontSize:11}}>{err}</p>
        : <div style={{display:"flex",flexDirection:"column",gap:4}}>
            {!done&&!text&&<Typing color={T.amberDim}/>}
            {text.split("\n").filter(Boolean).map((line,i) => {
              const col = line.startsWith("🔍")?T.amber : line.startsWith("⚠️")?T.err : line.startsWith("🔧")?T.good : line.startsWith("📊")?(/CRITICAL/.test(line)?T.crit:/WARNING/.test(line)?T.warn:T.good) : T.tx0;
              return <p key={i} style={{fontFamily:"'Space Mono',monospace",fontSize:11,color:col,lineHeight:1.8}}>{line}</p>;
            })}
          </div>
      }
    </Panel>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// ASK AI CHAT PANEL
// ─────────────────────────────────────────────────────────────────────────────
function AskPanel({ sourceId, logLines, onClose }) {
  const [msgs, setMsgs] = useState([
    { role:"ai", text:`Ready. Ask me anything about your **${sourceId}** logs — errors, patterns, fixes, or security issues.` }
  ]);
  const [input, setInput]       = useState("");
  const [busy,  setBusy]        = useState(false);
  const bottomRef = useRef(null);
  const inputRef  = useRef(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior:"smooth" }); }, [msgs]);

  const esRef = useRef(null);

  // Close any open stream when component unmounts
  useEffect(() => () => { esRef.current?.close(); }, []);

  const send = async () => {
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    setBusy(true);
    setMsgs(m => [...m, {role:"user",text:q}, {role:"ai",text:"",streaming:true}]);
    const ctx = (logLines||[]).slice(-80).join("\n").slice(0,4000);
    const url = `${API}/ask?source=${encodeURIComponent(sourceId)}&question=${encodeURIComponent(q)}&context=${encodeURIComponent(ctx)}`;
    esRef.current?.close();
    const es = new EventSource(url);
    esRef.current = es;
    let buf = "";
    es.onmessage = e => {
      if (e.data==="[DONE]") {
        setBusy(false);
        setMsgs(m => m.map((msg,i) => i===m.length-1 ? {...msg,streaming:false} : msg));
        es.close(); esRef.current = null; return;
      }
      buf += e.data;
      setMsgs(m => m.map((msg,i) => i===m.length-1 ? {...msg,text:buf} : msg));
    };
    es.onerror = () => {
      setBusy(false);
      setMsgs(m => m.map((msg,i) => i===m.length-1 ? {...msg,text:"⚠ Ollama not reachable — check that it is running on the host.",streaming:false} : msg));
      es.close(); esRef.current = null;
    };
  };

  const QUICK = [
    "What errors are most critical?",
    "Is there a security concern?",
    "How do I fix the top issue?",
    "What happened in the last hour?",
  ];

  return (
    <Panel color={T.ai} label="ASK AI" onClose={onClose} icon={<ChatIcon sz={14} col={T.ai}/>} noPad>
      {/* Messages */}
      <div style={{flex:1,overflowY:"auto",padding:"12px 14px",display:"flex",flexDirection:"column",gap:10}}>
        {msgs.map((msg,i) => (
          <div key={i} style={{display:"flex",flexDirection:"column",alignItems:msg.role==="user"?"flex-end":"flex-start",gap:3}}>
            <div style={{
              maxWidth:"90%",padding:"9px 13px",
              background:msg.role==="user" ? `${T.amber}15` : T.aiBg,
              border:`1px solid ${msg.role==="user" ? T.amberDim+"55" : T.aiDim+"44"}`,
              borderRadius:msg.role==="user" ? "12px 12px 3px 12px" : "3px 12px 12px 12px",
              fontFamily:"'Space Mono',monospace",fontSize:11,lineHeight:1.8,
              color:msg.role==="user" ? T.amber : T.tx0,
              whiteSpace:"pre-wrap",wordBreak:"break-word",
            }}>
              {msg.streaming && !msg.text ? <Typing color={T.aiDim}/> : msg.text}
            </div>
            <span style={{fontFamily:"'Space Mono',monospace",fontSize:8,color:T.tx2}}>
              {msg.role==="user" ? "you" : "axiom-ai"}
            </span>
          </div>
        ))}
        <div ref={bottomRef}/>
      </div>

      {/* Quick prompts */}
      {msgs.length <= 2 &&
        <div style={{display:"flex",gap:5,flexWrap:"wrap",padding:"0 14px 8px"}}>
          {QUICK.map(q =>
            <button key={q} onClick={()=>{ setInput(q); inputRef.current?.focus(); }}
              style={{fontFamily:"'Space Mono',monospace",fontSize:9,padding:"4px 10px",
                borderRadius:20,background:"transparent",
                border:`1px solid ${T.border2}`,color:T.tx1,cursor:"pointer",
                transition:"all .12s"}}
              onMouseEnter={e=>{e.currentTarget.style.borderColor=T.ai+"88";e.currentTarget.style.color=T.ai;}}
              onMouseLeave={e=>{e.currentTarget.style.borderColor=T.border2;e.currentTarget.style.color=T.tx1;}}>
              {q}
            </button>
          )}
        </div>
      }

      {/* Input row */}
      <div className="ask-input-row" style={{display:"flex",alignItems:"center",gap:8,padding:"10px 14px",borderTop:`1px solid ${T.border}`}}>
        <input ref={inputRef} value={input} onChange={e=>setInput(e.target.value)}
          onKeyDown={e=>{ if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();} }}
          placeholder="Ask about errors, root cause, fixes…"
          style={{flex:1,background:T.bg0,border:`1px solid ${T.border2}`,borderRadius:8,
            padding:"8px 12px",fontFamily:"'Space Mono',monospace",fontSize:11,
            color:T.tx0,outline:"none",transition:"border-color .15s"}}
          onFocus={e=>e.target.style.borderColor=T.ai+"88"}
          onBlur={e=>e.target.style.borderColor=T.border2}/>
        <button onClick={send} disabled={busy||!input.trim()}
          style={{width:36,height:36,borderRadius:8,display:"flex",alignItems:"center",justifyContent:"center",
            cursor:busy||!input.trim()?"not-allowed":"pointer",flexShrink:0,
            background:busy||!input.trim()?"transparent":`${T.ai}18`,
            border:`1px solid ${busy||!input.trim()?T.border:T.ai+"55"}`,
            transition:"all .12s"}}>
          <SendIcon sz={14} col={busy||!input.trim()?T.tx2:T.ai}/>
        </button>
      </div>
    </Panel>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SHARED PANEL WRAPPER
// ─────────────────────────────────────────────────────────────────────────────
function Panel({ color, label, onClose, icon, children, noPad }) {
  return (
    <div style={{border:`1px solid ${color}30`,borderRadius:10,overflow:"hidden",
      background:T.bg1,display:"flex",flexDirection:"column",marginBottom:8}}>
      <div style={{display:"flex",alignItems:"center",gap:8,padding:"9px 14px",
        borderBottom:`1px solid ${T.border}`,flexShrink:0,
        background:`${color}09`}}>
        {icon}
        <span style={{fontFamily:"'Syne',sans-serif",fontSize:10,fontWeight:700,
          color,letterSpacing:3}}>{label}</span>
        <button onClick={onClose}
          style={{marginLeft:"auto",background:"none",border:"none",cursor:"pointer",
            padding:4,display:"flex",alignItems:"center",justifyContent:"center",
            borderRadius:6,transition:"background .12s"}}
          onMouseEnter={e=>e.currentTarget.style.background=T.bg3}
          onMouseLeave={e=>e.currentTarget.style.background="transparent"}>
          <CloseIcon sz={14} col={T.tx2}/>
        </button>
      </div>
      <div style={noPad ? {flex:1,display:"flex",flexDirection:"column",overflow:"hidden"} : {padding:"10px 14px"}}>
        {children}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// TYPING INDICATOR
// ─────────────────────────────────────────────────────────────────────────────
function Typing({ color }) {
  return (
    <div style={{display:"flex",gap:4,alignItems:"center",height:18}}>
      {[0,.2,.4].map((d,i) =>
        <div key={i} style={{width:5,height:5,borderRadius:"50%",background:color||T.tx2,
          animation:`dot-bounce 1.2s ${d}s ease-in-out infinite`}}/>
      )}
      <style>{`@keyframes dot-bounce{0%,100%{transform:translateY(0);opacity:.3}50%{transform:translateY(-4px);opacity:1}}`}</style>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SYSMON INTERNAL LOG
// ─────────────────────────────────────────────────────────────────────────────
function SysmonLog() {
  const [lines, setLines] = useState([]);
  const [err, setErr]     = useState("");
  const load = () => fetch(`${API}/sysmon-logs`).then(r=>r.json()).then(d=>setLines(d.lines||[])).catch(e=>setErr(e.message));
  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);
  if (err) return <p style={{padding:24,color:T.err,fontFamily:"'Space Mono',monospace",fontSize:11}}>✗ {err}</p>;
  if (!lines.length) return <p style={{padding:48,textAlign:"center",color:T.tx2,fontFamily:"'Space Mono',monospace",fontSize:11}}>No activity yet</p>;
  return (
    <div style={{fontFamily:"'Space Mono',monospace",fontSize:10,lineHeight:1.75}}>
      {lines.map((l,i) => {
        const isErr = /\[error\]/i.test(l);
        return <div key={i} style={{padding:"1px 16px",color:isErr?T.err:T.tx1,borderLeft:`2px solid ${isErr?T.err+"55":"transparent"}`}}>{l}</div>;
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// LOGO SVG
// ─────────────────────────────────────────────────────────────────────────────
function AxiomLogo({ size=36 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" fill="none">
      <rect width="64" height="64" rx="11" fill={T.bg0}/>
      <polygon points="32,6 56,19 56,45 32,58 8,45 8,19"
               stroke={T.amber} strokeWidth="2.2" fill="none" strokeLinejoin="round"/>
      <line x1="17" y1="28" x2="47" y2="28" stroke={T.amber} strokeWidth="3.5" strokeLinecap="round"/>
      <line x1="17" y1="36" x2="41" y2="36" stroke={T.amber} strokeWidth="3.5" strokeLinecap="round" opacity=".55"/>
      <line x1="17" y1="44" x2="44" y2="44" stroke={T.amber} strokeWidth="3.5" strokeLinecap="round" opacity=".25"/>
      <circle cx="47" cy="22" r="4" fill={T.crit}/>
    </svg>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// MAIN APP
// ─────────────────────────────────────────────────────────────────────────────
export default function App() {
  const [activeId,    setActiveId]    = useState("syslog");
  const [logData,     setLogData]     = useState(null);
  const [loading,     setLoading]     = useState(false);
  const [err,         setErr]         = useState("");
  const [lineCount,   setLineCount]   = useState(200);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [aiMode,      setAiMode]      = useState(null); // null | "analyze" | "chat"
  const [aiKey,       setAiKey]       = useState(0);
  const [search,      setSearch]      = useState("");
  const [sideOpen,    setSideOpen]    = useState(true);
  const autoRef = useRef(null);
  const now     = useClock();

  const fetchLogs = useCallback(async (id, count) => {
    if (id==="_sysmon") return;
    setLoading(true); setErr("");
    try {
      const r = await fetch(`${API}/logs/${id}?lines=${count}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setLogData(await r.json());
    } catch(e) { setErr(e.message); }
    finally    { setLoading(false); }
  }, []);

  useEffect(() => { setLogData(null); setAiMode(null); setSearch(""); fetchLogs(activeId, lineCount); }, [activeId]);
  useEffect(() => { fetchLogs(activeId, lineCount); }, [lineCount]);
  useEffect(() => {
    clearInterval(autoRef.current);
    if (autoRefresh) autoRef.current = setInterval(() => fetchLogs(activeId, lineCount), 10_000);
    return () => clearInterval(autoRef.current);
  }, [autoRefresh, activeId, lineCount, fetchLogs]);
  useEffect(() => { window.__axiomHideSplash?.(); }, []);

  const src      = SOURCES.find(s => s.id===activeId);
  const entries  = logData?.entries || [];
  const filtered = useMemo(() =>
    search ? entries.filter(l => l.toLowerCase().includes(search.toLowerCase())) : entries,
    [entries, search]
  );
  const stats = useMemo(() => {
    const s = {critical:0,error:0,warn:0,good:0};
    filtered.forEach(l => { const t=classify(l); if(s[t]!==undefined) s[t]++; });
    return s;
  }, [filtered]);
  const canAI = activeId !== "_sysmon";

  return (
    <div style={{height:"100dvh",background:T.bg0,color:T.tx0,
      fontFamily:"'Space Mono',monospace",display:"flex",flexDirection:"column",
      overflow:"hidden"}}>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=Syne:wght@700;800&display=swap');
        *{box-sizing:border-box;margin:0;padding:0}
        ::-webkit-scrollbar{width:4px;height:4px}
        ::-webkit-scrollbar-track{background:${T.bg0}}
        ::-webkit-scrollbar-thumb{background:${T.border2};border-radius:4px}
        ::-webkit-scrollbar-thumb:hover{background:${T.tx2}}
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}
        @keyframes fade-in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
        @keyframes glow-crit{0%,100%{box-shadow:0 0 0 transparent}50%{box-shadow:0 0 6px ${T.crit}44}}
        .log-line{animation:fade-in .1s ease both}
        .nav-item{transition:background .1s,border-color .1s}
        .nav-item:hover{background:${T.bg3}!important;border-color:${T.border2}!important}
        .pill-btn{transition:all .15s}
        .pill-btn:hover{filter:brightness(1.2)}
        input::placeholder{color:${T.tx2}}
        button,a{-webkit-tap-highlight-color:transparent}

        /* Scanline texture on log area */
        .log-area{
          position:relative;
        }
        .log-area::before{
          content:"";position:absolute;inset:0;pointer-events:none;z-index:1;
          background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.04) 2px,rgba(0,0,0,.04) 4px);
        }

        /* ── Android / Mobile PWA ── */
        .bottom-nav{display:none}
        .top-controls-mobile{display:none}
        @media(max-width:700px){
          .side-rail{display:none!important}
          .bottom-nav{display:flex!important}
          .logo-text{display:none!important}
          .top-controls-full{display:none!important}
          .top-controls-mobile{display:flex!important}
          .toolbar-row{gap:6px!important}
          .lines-select{display:none!important}
          .log-font{font-size:12px!important;line-height:1.8!important}
          .log-gutter{width:34px!important;padding-right:6px!important;padding-left:4px!important;font-size:8px!important}
          .log-text{padding-left:8px!important;padding-right:8px!important}
          .footer-bar{display:none!important}
          .ai-panel-wrap{padding:6px 8px 0!important}
          .ask-input-row{padding:8px 10px env(safe-area-inset-bottom,8px)!important}
          .search-box{flex:1!important;min-width:0!important}
          .sev-chip-label{display:none!important}
          .header-root{height:52px!important}
        }
      `}</style>

      {/* ════════════════════════════════════════════════════════════ TOP BAR */}
      <header className="header-root" style={{
        height:54,flexShrink:0,
        display:"flex",alignItems:"center",
        borderBottom:`1px solid ${T.border}`,
        background:T.bg1,
        paddingRight:14,
      }}>
        {/* Logo + brand */}
        <div style={{display:"flex",alignItems:"center",gap:12,
          padding:"0 16px",height:"100%",borderRight:`1px solid ${T.border}`}}>
          <AxiomLogo size={32}/>
          <div className="logo-text">
            <div style={{fontFamily:"'Syne',sans-serif",fontSize:15,fontWeight:800,
              color:T.amber,letterSpacing:4}}>AXIOM</div>
            <div style={{fontSize:7,color:T.tx2,letterSpacing:4,marginTop:1}}>LOG SERVER</div>
          </div>
        </div>

        {/* Source breadcrumb */}
        {src && (
          <div style={{display:"flex",alignItems:"center",gap:8,marginLeft:16}}>
            <src.icon sz={14} col={src.color}/>
            <span style={{fontFamily:"'Syne',sans-serif",fontSize:11,fontWeight:700,
              color:src.color,letterSpacing:2}}>{src.label}</span>
          </div>
        )}

        {/* Spacer */}
        <div style={{flex:1}}/>

        {/* Mobile: compact AI + refresh buttons */}
        <div className="top-controls-mobile" style={{gap:6,alignItems:"center",marginRight:4}}>
          {canAI && (
            <>
              <IconBtn
                active={aiMode==="analyze"} color={T.amber}
                onClick={() => { setAiMode(m => m==="analyze"?null:"analyze"); setAiKey(k=>k+1); }}
                title="Analyze">
                <AiIcon sz={16} col={aiMode==="analyze"?T.amber:T.tx1}/>
              </IconBtn>
              <IconBtn
                active={aiMode==="chat"} color={T.ai}
                onClick={() => setAiMode(m => m==="chat"?null:"chat")}
                title="Ask AI">
                <ChatIcon sz={16} col={aiMode==="chat"?T.ai:T.tx1}/>
              </IconBtn>
            </>
          )}
          <IconBtn active={autoRefresh} color={T.good}
            onClick={() => setAutoRefresh(a=>!a)} title={autoRefresh?"Pause":"Live"}>
            <LiveIcon sz={16} col={autoRefresh?T.good:T.tx1}/>
          </IconBtn>
          <IconBtn active={false} color={T.tx1}
            onClick={() => fetchLogs(activeId, lineCount)} title="Refresh">
            <RefreshIcon sz={16} col={T.tx1}/>
          </IconBtn>
        </div>

        {/* Desktop: Clock */}
        <div className="top-controls-full" style={{marginRight:16,fontFamily:"'Space Mono',monospace",fontSize:10,
          color:T.tx1,letterSpacing:1,display:"flex",alignItems:"center",gap:10}}>
          <span style={{color:T.tx0}}>{now.toLocaleTimeString()}</span>
          <span style={{color:T.tx2,fontSize:9}}>{now.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"})}</span>
        </div>

        {/* Desktop: Controls */}
        <div className="top-controls-full" style={{display:"flex",gap:6,alignItems:"center"}}>
          <PillBtn
            active={autoRefresh}
            color={autoRefresh ? T.good : T.tx1}
            onClick={() => setAutoRefresh(a=>!a)}
            title={autoRefresh ? "Pause auto-refresh" : "Enable live refresh"}>
            <LiveIcon sz={13} col={autoRefresh?T.good:T.tx1}/>
            <span>{autoRefresh?"LIVE":"AUTO"}</span>
          </PillBtn>

          {canAI && (
            <>
              <PillBtn
                active={aiMode==="analyze"}
                color={T.amber}
                onClick={() => { setAiMode(m => m==="analyze"?null:"analyze"); setAiKey(k=>k+1); }}
                title="Auto-analyze logs with AI">
                <AiIcon sz={13} col={aiMode==="analyze"?T.amber:T.tx1}/>
                <span>ANALYZE</span>
              </PillBtn>

              <PillBtn
                active={aiMode==="chat"}
                color={T.ai}
                onClick={() => setAiMode(m => m==="chat"?null:"chat")}
                title="Chat with AI about logs">
                <ChatIcon sz={13} col={aiMode==="chat"?T.ai:T.tx1}/>
                <span>ASK AI</span>
              </PillBtn>
            </>
          )}

          <PillBtn active={false} color={T.tx1}
            onClick={() => fetchLogs(activeId, lineCount)} title="Refresh">
            <RefreshIcon sz={13} col={T.tx1}/>
          </PillBtn>
        </div>
      </header>

      {/* ═══════════════════════════════════════════════════════════ BODY */}
      <div style={{display:"flex",flex:1,overflow:"hidden"}}>

        {/* ═══════════════════════════════════════════ SIDEBAR */}
        <nav className="side-rail" style={{
          width:sideOpen?188:52,flexShrink:0,
          borderRight:`1px solid ${T.border}`,
          background:T.bg1,
          display:"flex",flexDirection:"column",
          overflowY:"auto",overflowX:"hidden",
          padding:"10px 6px",
          transition:"width .2s ease",
        }}>
          <div className="side-label" style={{fontSize:7,color:T.tx2,letterSpacing:3,
            padding:"2px 8px 10px",textTransform:"uppercase",opacity:sideOpen?1:0,
            transition:"opacity .15s",whiteSpace:"nowrap"}}>Sources</div>

          {SOURCES.map((s, idx) => {
            const active = s.id===activeId;
            const Ic = s.icon;
            return (
              <div key={s.id}>
                {s.divider && <div style={{margin:"8px 6px",borderTop:`1px solid ${T.border}`}}/>}
                <button className="nav-item"
                  onClick={() => setActiveId(s.id)}
                  title={s.label}
                  style={{
                    display:"flex",alignItems:"center",
                    gap:sideOpen?10:0,
                    justifyContent:sideOpen?"flex-start":"center",
                    padding:sideOpen?"8px 10px":"8px",
                    borderRadius:8,marginBottom:2,width:"100%",
                    background:active?`${s.color}10`:"transparent",
                    border:`1px solid ${active?s.color+"38":"transparent"}`,
                    color:active?s.color:T.tx1,
                    cursor:"pointer",textAlign:"left",
                    transition:"all .12s",
                  }}>
                  {/* Icon box */}
                  <div style={{
                    width:30,height:30,borderRadius:7,flexShrink:0,
                    background:active?`${s.color}18`:T.bg2,
                    border:`1px solid ${active?s.color+"44":T.border}`,
                    display:"flex",alignItems:"center",justifyContent:"center",
                    transition:"all .12s",
                  }}>
                    <Ic sz={15} col={active?s.color:T.tx2}/>
                  </div>

                  {sideOpen && (
                    <div style={{flex:1,minWidth:0,overflow:"hidden"}}>
                      <div className="side-label" style={{fontFamily:"'Syne',sans-serif",fontSize:11,fontWeight:700,
                        letterSpacing:.5,whiteSpace:"nowrap"}}>{s.label}</div>
                      <div className="side-desc" style={{fontFamily:"'Space Mono',monospace",fontSize:8,
                        color:active?`${s.color}88`:T.tx2,marginTop:2,
                        overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{s.desc}</div>
                    </div>
                  )}

                  {active && sideOpen && (
                    <div style={{width:2,height:16,borderRadius:1,
                      background:s.color,boxShadow:`0 0 6px ${s.color}88`,flexShrink:0}}/>
                  )}
                </button>
              </div>
            );
          })}

          {/* Collapse toggle */}
          <div style={{flex:1}}/>
          <button onClick={()=>setSideOpen(o=>!o)}
            title={sideOpen?"Collapse sidebar":"Expand sidebar"}
            style={{display:"flex",alignItems:"center",justifyContent:"center",
              margin:"6px 2px 0",padding:8,borderRadius:7,
              background:"transparent",border:`1px solid ${T.border}`,
              color:T.tx2,cursor:"pointer",transition:"all .12s",width:"calc(100% - 4px)"}}
            onMouseEnter={e=>{e.currentTarget.style.background=T.bg3;e.currentTarget.style.color=T.tx1;}}
            onMouseLeave={e=>{e.currentTarget.style.background="transparent";e.currentTarget.style.color=T.tx2;}}>
            <svg width={14} height={14} viewBox="0 0 16 16" fill="none">
              <polyline points={sideOpen?"11,4 5,8 11,12":"5,4 11,8 5,12"}
                stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>
        </nav>

        {/* ═══════════════════════════════════════════════ MAIN */}
        <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>

          {/* ──────────────────────────────── TOOLBAR */}
          <div className="toolbar-row" style={{
            display:"flex",alignItems:"center",gap:8,flexWrap:"wrap",
            padding:"7px 14px",borderBottom:`1px solid ${T.border}`,
            background:T.bg1,flexShrink:0,
          }}>
            {/* Severity chips */}
            {stats.critical>0 && <SevChip col={T.crit}  label="CRIT"  n={stats.critical}/>}
            {stats.error>0    && <SevChip col={T.err}   label="ERR"   n={stats.error}/>}
            {stats.warn>0     && <SevChip col={T.warn}  label="WARN"  n={stats.warn}/>}

            {logData && <span style={{fontSize:9,color:T.tx2}}>
              {filtered.length}{search?` / ${entries.length}`:""} lines
            </span>}

            {/* Severity bar */}
            {logData && <SeverityBar stats={stats}/>}

            {/* Push right */}
            <div style={{marginLeft:"auto",display:"flex",gap:8,alignItems:"center"}}>
              {/* Search */}
              <div className="search-box" style={{display:"flex",alignItems:"center",gap:8,
                background:T.bg0,border:`1px solid ${T.border2}`,borderRadius:8,
                padding:"5px 12px",transition:"border-color .15s",minWidth:160}}
                onFocus={()=>{}} >
                <SearchIcon sz={13} col={T.tx2}/>
                <input value={search} onChange={e=>setSearch(e.target.value)}
                  placeholder="filter…"
                  style={{background:"none",border:"none",outline:"none",
                    color:T.tx0,fontSize:11,width:"100%",minWidth:0,fontFamily:"'Space Mono',monospace"}}/>
                {search && <button onClick={()=>setSearch("")}
                  style={{background:"none",border:"none",color:T.tx2,cursor:"pointer",fontSize:14,lineHeight:1,padding:0}}>×</button>}
              </div>

              {/* Lines selector */}
              <select className="lines-select" value={lineCount}
                onChange={e=>{const n=+e.target.value;setLineCount(n);fetchLogs(activeId,n);}}
                style={{background:T.bg0,border:`1px solid ${T.border2}`,color:T.tx1,
                  borderRadius:8,padding:"5px 10px",fontSize:10,cursor:"pointer",
                  fontFamily:"'Space Mono',monospace",outline:"none"}}>
                {[100,200,500,1000,2000].map(n=><option key={n} value={n}>{n} lines</option>)}
              </select>
            </div>
          </div>

          {/* ──────────────────────────────── AI PANELS */}
          {aiMode && canAI && (
            <div className="ai-panel-wrap" style={{padding:"8px 14px 0",flexShrink:0}}>
              {aiMode==="analyze" && <AnalysisPanel key={aiKey} sourceId={activeId} onClose={()=>setAiMode(null)}/>}
              {aiMode==="chat"    && <AskPanel sourceId={activeId} logLines={entries} onClose={()=>setAiMode(null)}/>}
            </div>
          )}

          {/* ──────────────────────────────── LOG STREAM */}
          <div className="log-area" style={{flex:1,overflowY:"auto",position:"relative"}}>
            {loading && (
              <div style={{display:"flex",alignItems:"center",justifyContent:"center",
                height:120,gap:10,color:T.amberDim,fontFamily:"'Space Mono',monospace",fontSize:11}}>
                <Typing color={T.amberDim}/>&nbsp;Loading…
              </div>
            )}

            {err && !loading && (
              <div style={{margin:16,padding:"12px 16px",
                background:"rgba(248,113,113,0.08)",border:`1px solid ${T.crit}30`,
                borderRadius:8,fontFamily:"'Space Mono',monospace",fontSize:11,color:T.err}}>
                ✗ {err}
              </div>
            )}

            {!loading && activeId==="smart"    && <SmartPanel data={logData}/>}
            {!loading && activeId==="_sysmon"  && <SysmonLog/>}

            {!loading && !err && activeId!=="smart" && activeId!=="_sysmon" && (
              filtered.length===0
                ? <p style={{padding:48,textAlign:"center",color:T.tx2,
                    fontFamily:"'Space Mono',monospace",fontSize:11}}>
                    {search?"No lines match that filter":"No log entries found"}
                  </p>
                : <div className="log-font" style={{fontFamily:"'Space Mono',monospace",fontSize:10.5,lineHeight:1.7}}>
                    {filtered.map((line,i) => {
                      const t = classify(line);
                      const s = L[t];
                      return (
                        <div key={i} className="log-line"
                          style={{
                            display:"flex",
                            background:s.bg,
                            borderLeft:`2px solid ${s.bar}`,
                            color:s.color,
                          }}>
                          {/* Line number gutter */}
                          <span className="log-gutter" style={{
                            flexShrink:0,width:46,textAlign:"right",
                            paddingRight:12,paddingLeft:8,
                            color:T.tx2,fontSize:9,userSelect:"none",
                            borderRight:`1px solid ${T.border}`,
                            lineHeight:1.7,
                          }}>{i+1}</span>
                          <span className="log-text" style={{
                            flex:1,paddingLeft:12,paddingRight:14,
                            whiteSpace:"pre-wrap",wordBreak:"break-all",
                          }}>{line}</span>
                        </div>
                      );
                    })}
                  </div>
            )}
          </div>

          {/* ──────────────────────────────── STATUS BAR */}
          <footer className="footer-bar" style={{
            display:"flex",alignItems:"center",justifyContent:"space-between",
            padding:"4px 14px",
            borderTop:`1px solid ${T.border}`,
            background:T.bg1,flexShrink:0,
            fontFamily:"'Space Mono',monospace",fontSize:9,color:T.tx2,
          }}>
            <div style={{display:"flex",alignItems:"center",gap:10}}>
              <AxiomLogo size={16}/>
              <span style={{fontFamily:"'Syne',sans-serif",fontWeight:800,color:T.amberDim,letterSpacing:3,fontSize:8}}>AXIOM</span>
              <span>v1.0</span>
              {autoRefresh && <span style={{color:T.good}}><span style={{animation:"pulse 1.4s infinite"}}>●</span> live</span>}
            </div>
            <div style={{display:"flex",gap:14}}>
              {logData?.fetched_at && <span>↑ {new Date(logData.fetched_at).toLocaleTimeString()}</span>}
              <span style={{color:stats.critical>0?T.crit:T.tx2}}>{stats.critical} crit</span>
              <span style={{color:stats.error>0?T.err:T.tx2}}>{stats.error} err</span>
              <span style={{color:stats.warn>0?T.warn:T.tx2}}>{stats.warn} warn</span>
              <span>{filtered.length} lines</span>
            </div>
          </footer>
        </div>
      </div>

      {/* ════════════════════════════════════════════ BOTTOM NAV (mobile only) */}
      <nav className="bottom-nav" style={{
        flexShrink:0,
        borderTop:`1px solid ${T.border}`,
        background:T.bg1,
        alignItems:"stretch",
        paddingBottom:"env(safe-area-inset-bottom,0px)",
        zIndex:100,
      }}>
        {SOURCES.map((s) => {
          const active = s.id===activeId;
          const Ic = s.icon;
          return (
            <button key={s.id}
              onClick={() => setActiveId(s.id)}
              style={{
                flex:1, display:"flex", flexDirection:"column",
                alignItems:"center", justifyContent:"center",
                gap:3, padding:"8px 2px 6px",
                background:"transparent", border:"none",
                cursor:"pointer",
                borderTop:`2px solid ${active ? s.color : "transparent"}`,
                transition:"all .12s",
                minWidth:0,
              }}>
              <div style={{
                width:28,height:28,borderRadius:7,
                background:active?`${s.color}18`:T.bg2,
                display:"flex",alignItems:"center",justifyContent:"center",
                transition:"all .12s",
              }}>
                <Ic sz={14} col={active?s.color:T.tx2}/>
              </div>
              <span style={{
                fontFamily:"'Space Mono',monospace",fontSize:7,
                color:active?s.color:T.tx2,
                letterSpacing:.3,
                whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis",
                maxWidth:"100%",
              }}>{s.label}</span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// REUSABLE ATOMS
// ─────────────────────────────────────────────────────────────────────────────
function PillBtn({ active, color, onClick, title, children }) {
  return (
    <button className="pill-btn" onClick={onClick} title={title}
      style={{
        display:"flex",alignItems:"center",gap:5,
        padding:"5px 11px",borderRadius:7,cursor:"pointer",
        background:active?`${color}15`:"transparent",
        border:`1px solid ${active?color+"55":T.border2}`,
        color:active?color:T.tx1,
        fontFamily:"'Space Mono',monospace",fontSize:9,letterSpacing:.5,
      }}>
      {children}
    </button>
  );
}

function IconBtn({ active, color, onClick, title, children }) {
  return (
    <button onClick={onClick} title={title}
      style={{
        width:40,height:40,borderRadius:9,
        display:"flex",alignItems:"center",justifyContent:"center",
        cursor:"pointer",
        background:active?`${color}18`:"transparent",
        border:`1px solid ${active?color+"55":T.border2}`,
        transition:"all .12s",flexShrink:0,
      }}>
      {children}
    </button>
  );
}

function SevChip({ col, label, n }) {
  return (
    <div style={{
      display:"flex",alignItems:"center",gap:4,padding:"2px 9px",
      borderRadius:20,background:`${col}12`,border:`1px solid ${col}38`,
      fontFamily:"'Space Mono',monospace",fontSize:8,color:col,letterSpacing:.5,
    }}>
      <div style={{width:5,height:5,borderRadius:"50%",background:col,animation:col===T.crit?"glow-crit 1.8s infinite":undefined}}/>
      <span className="sev-chip-label">{label}</span> <b>{n}</b>
    </div>
  );
}
