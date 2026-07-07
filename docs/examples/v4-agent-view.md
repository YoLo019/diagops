# V4 Agent View Example

Start the backend, trigger a simulated incident, then copy the returned `id`:

```bash
curl -X POST http://127.0.0.1:8000/events/simulated/deployment_regression
```

Use that investigation ID to inspect the Agent process:

```bash
curl http://127.0.0.1:8000/investigations/<id>/plan
curl http://127.0.0.1:8000/investigations/<id>/tasks
curl http://127.0.0.1:8000/investigations/<id>/agent-executions
curl http://127.0.0.1:8000/investigations/<id>/context
curl http://127.0.0.1:8000/investigations/<id>/tool-calls
curl http://127.0.0.1:8000/investigations/<id>/memory
curl http://127.0.0.1:8000/investigations/<id>/task-graph
```

Response shapes:

```json
{
  "plan": {
    "id": "plan-...",
    "investigation_id": "<id>",
    "tasks": [
      {
        "id": "task-...",
        "title": "Read logs",
        "task_type": "log_investigation",
        "agent_name": "LogAgent",
        "tool_names": ["log_file"],
        "depends_on": [],
        "status": "completed"
      }
    ]
  },
  "agent_executions": [
    {
      "id": "exec-...",
      "task_id": "task-...",
      "agent_name": "LogAgent",
      "status": "completed",
      "tool_call_ids": ["tool-..."],
      "evidence_ids": ["ev-..."],
      "summary": "..."
    }
  ],
  "context": [
    {
      "id": "fact-...",
      "source_agent": "LogAgent",
      "fact_type": "observation",
      "summary": "...",
      "confidence": 0.8,
      "evidence_ids": ["ev-..."]
    }
  ],
  "tool_calls": [
    {
      "id": "tool-...",
      "task_id": "task-...",
      "agent_name": "LogAgent",
      "tool_name": "log_file",
      "input": {"investigation_id": "<id>"},
      "status": "success",
      "output_evidence_ids": ["ev-..."]
    }
  ],
  "memory": [
    {
      "id": "mem-...",
      "service": "checkout-service",
      "environment": "prod",
      "memory_type": "human_feedback",
      "summary": "..."
    }
  ],
  "task_graph": {
    "nodes": [
      {
        "id": "task-...",
        "label": "Read logs",
        "type": "log_investigation",
        "status": "completed",
        "agent_name": "LogAgent"
      }
    ],
    "edges": [{"source": "task-a", "target": "task-b"}]
  }
}
```
