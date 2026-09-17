"""Record a trace from the REAL Python engine, for the Swift contract test to decode."""
import json, pathlib, sys, tempfile
sys.path.insert(0, '.')
from engine.bus import EventBus
from engine.protocol import Command, CommandType, Ack, EventType
from engine.state import Workspace
from engine.config import load
from engine.library import resolve
from engine.orchestrator import Orchestrator

out = pathlib.Path('macos/Tests/AgentOrgKitTests/Fixtures/engine-trace.jsonl')
out.parent.mkdir(parents=True, exist_ok=True)
if out.exists(): out.unlink()

with tempfile.TemporaryDirectory() as td:
    ws = Workspace.for_project('fixture', root=pathlib.Path(td)/'projects'); ws.ensure()
    bus = EventBus(run_id="run_fixture", trace_path=ws.trace_path)
    # Emit a representative slice of the lifecycle through the real bus, so the wire shape is genuine.
    bus.emit(EventType.RUN_START, payload={'run_id':'run_fixture','workflow':'fixture','pid':1234})
    bus.emit(EventType.MANIFEST_PROPOSED, payload={'slug':'fixture','validated':True,
        'nodes':['pm','architect','developer','reviewer'],'gates':['release'],
        'loops':[{'id':'review-fix-loop','max_iterations':3}],'staffing_gaps':[]})
    bus.emit(EventType.MANIFEST_APPROVED, payload={'nodes':['pm','developer','reviewer']})
    bus.emit(EventType.AGENT_SPAWN, agent_id='ag_7f3a', payload={'name':'Alice','skill':'backend-developer','provider':'ollama','model':'qwen2.5-coder:7b'})
    bus.emit(EventType.NODE_ENTER, node_id='developer', phase='BUILD', payload={'phase':'BUILD'})
    bus.emit(EventType.LLM_REQUEST, agent_id='ag_7f3a', node_id='developer',
             session_id='ses_001', payload={'provider':'ollama','model':'qwen2.5-coder:7b','estimated_prompt_tokens':4200,'stream':False})
    bus.emit(EventType.LLM_RESPONSE, agent_id='ag_7f3a', node_id='developer', session_id='ses_001',
             payload={'provider':'ollama','model':'qwen2.5-coder:7b',
                      'usage':{'prompt_tokens':4100,'completion_tokens':260,'total_tokens':4360,'measured':True},
                      'cost':{'usd':0.0,'source':'free','known':True},'latency_ms':5120,'finish_reason':'stop'})
    bus.emit(EventType.SESSION_SATURATION, agent_id='ag_7f3a', session_id='ses_001',
             payload={'saturation':0.72,'band':'warning','projected':23600})
    bus.emit(EventType.SESSION_COMPACT, agent_id='ag_7f3a', session_id='ses_001',
             payload={'band':'warning','recovered':1800,'preserved_verbatim':3,'reverted':False})
    bus.emit(EventType.SESSION_ROTATE_REQUESTED, agent_id='ag_7f3a', session_id='ses_001',
             payload={'trigger':'capacity','saturation':0.88,'reason':'compaction exhausted'})
    bus.emit(EventType.ARTIFACT_WRITTEN, node_id='developer',
             payload={'type':'change','path':'src/app.py','sha256':'a'*64,'bytes':412})
    bus.emit(EventType.CHECKLIST_RESULT, node_id='developer',
             payload={'items':[{'id':'CR1','status':'PASS','evidence':'src/app.py#a'},
                               {'id':'CR2','status':'FAIL','evidence':'1 Critical unresolved'}]})
    bus.emit(EventType.REVIEW_REJECTED, node_id='reviewer',
             payload={'attempt':1,'summary':'SQL injection in the auth path',
                      'findings':[{'id':'F1','severity':'Critical','dimension':'security',
                                   'owasp':'A03:2021','file':'src/app.py','line':47,
                                   'issue':'userId interpolated','fix':'bind as a parameter'}]})
    bus.emit(EventType.ROUTE_DECIDED, payload={'node_id':'reviewer','skill':'code-reviewer',
             'route_class':'R-CONTRACT','autonomy':'auto','decided_by':'org','chosen':'ag_9c1d',
             'proposed':False,'candidates':[{'agent_id':'ag_9c1d','score':0.81}]})
    bus.emit(EventType.HANDOFF_VERIFIED, payload={'from':'developer','to':'reviewer','sha':'b'*16})
    bus.emit(EventType.AGENT_SPAWN_REQUESTED, payload={'tier':'T2','reason':'durable capability gap',
             'requisition':{'kind':'specialist','skill':'devops-engineer','expected_outcome':'unblock'}})
    bus.emit(EventType.GUARDRAIL_ON_EDGE if False else EventType.AGENT_SLO_BREACH,
             agent_id='ag_7f3a', payload={'objective':'agent_success','attainment':0.62,'severity':'warning'})
    bus.emit(EventType.COST_RECONCILED, payload={'estimated_tokens':4200,'actual_tokens':4100,'error_pct':2.4})
    bus.emit(EventType.HUMAN_GATE, node_id='release',
             payload={'gate_id':'release','kind':'human','reason':'Owner release approval',
                      'requires':['change'],'present':['change'],'missing':[]})
    bus.emit(EventType.HUMAN_DECISION, payload={'gate_id':'release','approved':True,'note':'ship it'})
    # The goal and subagent lifecycle, so the Swift side's summaries and decoding are exercised by the
    # same real frames the engine emits — a hand-written fixture would not catch a renamed field.
    bus.emit(EventType.GOAL_ARMED, payload={'objective':'add cursor pagination','by':'cli',
                                            'token_budget':0})
    bus.emit(EventType.GOAL_PROGRESS, payload={'objective':'add cursor pagination','round':1,
                                               'spend':{'rounds':1,'tokens':4100,'requests':1,
                                                        'cost_usd':0.0}})
    bus.emit(EventType.SUBAGENT_SPAWNED, payload={'child_id':'sub_001','agent_id':'ag_9c1d',
                                                  'skill':'code-reviewer','depth':1,'budget':0})
    bus.emit(EventType.SUBAGENT_DONE, payload={'child_id':'sub_001','status':'done','steps':3})
    bus.emit(EventType.SUBAGENT_READ, payload={'child_id':'sub_001','offset_bytes':0,
                                               'returned_bytes':8192,'total_bytes':2355})
    bus.emit(EventType.SUBAGENT_FAILED, payload={'child_id':'sub_002',
                                                 'error':'provider exploded'})
    bus.emit(EventType.GOAL_PAUSED, payload={'objective':'add cursor pagination','reason':'budget_spend',
                                             'spend':{'rounds':2,'tokens':9200,'requests':3,
                                                      'cost_usd':0.04}})
    bus.emit(EventType.GOAL_RESUMED, payload={'objective':'add cursor pagination','by':'cli'})
    bus.emit(EventType.GOAL_COMPLETED, payload={'objective':'add cursor pagination',
                                                'summary':'cursor pagination added, 12 tests pass',
                                                'spend':{'rounds':2,'tokens':9200,'requests':3,
                                                         'cost_usd':0.04}})
    bus.emit(EventType.GOAL_CLEARED, payload={'spend':{'rounds':2,'tokens':9200,'requests':3,
                                                       'cost_usd':0.04}})
    # A command ack, so the Swift side's correlation path is exercised by a real frame.
    ack = Ack(cmd_id='cmd_0000000000001_ab12', ok=True, detail={'accepted': True})
    bus.emit(EventType.COMMAND_ACK, event=ack.to_event(seq=0))
    bus.emit(EventType.ERROR, payload={'kind':'rate_limit','message':'slow down','status':429,
                                       'retryable':True,'needs_compaction':False})
    bus.emit(EventType.RUN_END, payload={'run_id':'run_fixture','outcome':'complete','steps_used':5,
                                         'iterations':{'review-fix-loop':2},'ok':True})
    bus.close()

    # The trace is what the app will actually read; verify it parses and has the fields we assert.
    lines = ws.trace_path.read_text().splitlines()
    out.write_text("\n".join(lines) + "\n")
    print(f'wrote {len(lines)} events to {out}')
    types = [json.loads(l)['type'] for l in lines]
    print('types:', types)
