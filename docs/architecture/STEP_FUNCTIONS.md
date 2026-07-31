# Step Functions Implementation

## Overview

polyris generates AWS Step Functions JSON (Amazon States Language) from Python DAG definitions.

---

## Generated Pipeline Structure

```json
{
  "Comment": "Pipeline: my-pipeline",
  "QueryLanguage": "JSONata",
  "StartAt": "Define_Inputs",
  "States": {
    "Define_Inputs": {
      "Type": "Pass",
      "Output": {
        "current_date": "{% $substringBefore($now(), 'T') %}",
        "variables": "{% $states.input.variables %}"
      },
      "Next": "Register_Pipeline"
    },
    
    "Register_Pipeline": {
      "Type": "Task",
      "Resource": "arn:aws:states:::dynamodb:putItem",
      "Arguments": {
        "TableName": "${registry_table}",
        "Item": {"pipeline_name": {"S": "..."}, ...}
      },
      "Next": "Save_DAG_Snapshot"
    },
    
    "Save_DAG_Snapshot": {
      "Type": "Task",
      "Comment": "Snapshot DAG structure for this execution (survives redeploys)",
      "Resource": "arn:aws:states:::dynamodb:putItem",
      "Arguments": {
        "TableName": "${tokens_table}",
        "Item": {
          "execution_name": {"S": "dag_snapshot::{execution_name}"},
          "dag": {"S": "{...dag structure...}"},
          "ttl": {"N": "...120 days..."}
        }
      },
      "Next": "Check_Register_Only"
    },
    
    "Check_Register_Only": {
      "Type": "Choice",
      "Choices": [{"Variable": "$.register_only", "BooleanEquals": true, "Next": "Success"}],
      "Default": "Run_All_Tasks"
    },
    
    "Run_All_Tasks": {
      "Type": "Parallel",
      "Branches": [
        {
          "StartAt": "Task_scrape",
          "States": {
            "Task_scrape": {
              "Type": "Task",
              "Resource": "arn:aws:states:::states:startExecution.waitForTaskToken",
              "Arguments": {
                "StateMachineArn": "${wrapper_arn}",
                "Input": {
                  "task_name": "scrape",
                  "task_arn": "...",
                  "dependencies": [],
                  "alerts": {"slack": "#ch"},
                  "token": "{% $states.context.Task.Token %}"
                }
              },
              "End": true
            }
          }
        },
        {
          "StartAt": "Task_process",
          "States": {...}
        }
      ],
      "Next": "Done"
    },
    
    "Done": {
      "Type": "Succeed"
    }
  }
}
```

---

## Task Types in ASL

### SFN Task

```json
{
  "Type": "Task",
  "Resource": "arn:aws:states:::states:startExecution.sync:2",
  "Arguments": {
    "StateMachineArn": "{% $states.input.task_arn %}",
    "Input": "{% $states.input %}"
  }
}
```

### Lambda Task

```json
{
  "Type": "Task",
  "Resource": "arn:aws:states:::lambda:invoke",
  "Arguments": {
    "FunctionName": "{% $states.input.task_config.function_name %}",
    "Payload": "{% $states.input %}"
  }
}
```

### Glue Task

```json
{
  "Type": "Task",
  "Resource": "arn:aws:states:::glue:startJobRun.sync",
  "Arguments": {
    "JobName": "{% $states.input.task_config.job_name %}",
    "Arguments": "{% $states.input.task_config.arguments %}"
  }
}
```

### ECS Task

```json
{
  "Type": "Task",
  "Resource": "arn:aws:states:::ecs:runTask.sync",
  "Arguments": {
    "Cluster": "{% $states.input.task_config.cluster %}",
    "TaskDefinition": "{% $states.input.task_config.task_definition %}",
    "LaunchType": "{% $states.input.task_config.launch_type %}",
    "NetworkConfiguration": {...}
  }
}
```

---

## Dependency Wrapper Flow

```
┌─────────────────────────────────────────────────────────────┐
│                   dependency_wrapper                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Generate_IDs                                               │
│       │                                                     │
│       ▼                                                     │
│  Call registration_helper                                   │
│       │                                                     │
│       ▼                                                     │
│  Check_Skip_Signal ──────────────▶ Already_Skipped         │
│       │                                   │                 │
│       ▼                                   │                 │
│  Check_Has_Dependencies                   │                 │
│       │                                   │                 │
│  ┌────┴────┐                              │                 │
│  │         │                              │                 │
│  Yes       No                             │                 │
│  │         │                              │                 │
│  ▼         │                              │                 │
│  Wait_For_Task                            │                 │
│  (waitForTaskToken)                       │                 │
│  │         │                              │                 │
│  │         ▼                              │                 │
│  │    Check_Deps_Signal                   │                 │
│  │         │                              │                 │
│  │    ┌────┼────┐                         │                 │
│  │    │    │    │                         │                 │
│  │   pass skip block                      │                 │
│  │    │    │    │                         │                 │
│  │    │    ▼    ▼                         │                 │
│  │    │  Mark_Skipped ◀───────────────────┤                 │
│  │    │    │                              │                 │
│  │    │    ▼                              │                 │
│  │    │  Emit_Skip_Event                  │                 │
│  │    │    │                              │                 │
│  │    │    ▼                              │                 │
│  │    │  Done ◀───────────────────────────┘                 │
│  │    │                                                     │
│  │    ▼                                                     │
│  └──▶ Check_Wait_Before                                     │
│           │                                                 │
│       ┌───┴───┐                                            │
│       │       │                                            │
│      Yes      No                                           │
│       │       │                                            │
│       ▼       │                                            │
│     Wait      │                                            │
│       │       │                                            │
│       └───┬───┘                                            │
│           │                                                 │
│           ▼                                                 │
│    Call run_task_helper                                     │
│           │                                                 │
│    ┌──────┴──────┐                                         │
│    │             │                                         │
│  Success      Failure                                      │
│    │             │                                         │
│    │             ▼                                         │
│    │     Call failure_handler                              │
│    │             │                                         │
│    │             ▼                                         │
│    │         Fail_State                                    │
│    │                                                       │
│    ▼                                                       │
│  Emit_Asset_Events (if outlets)                            │
│    │                                                       │
│    ▼                                                       │
│  Done                                                      │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

---

## JSONata Patterns

### Get current date
```
{% $substringBefore($now(), 'T') %}
```

### Conditional value
```
{% $exists($states.input.foo) ? $states.input.foo : 'default' %}
```

### String concatenation
```
{% 'prefix-' & $states.input.name & '-suffix' %}
```

### Array operations
```
{% $filter($states.input.items, function($v) { $v.status = 'active' }) %}
```

### Object merge
```
{% $merge([$states.input, {'new_field': 'value'}]) %}
```

---

## Error Handling

### Catch All Errors

```json
{
  "Type": "Task",
  "Resource": "...",
  "Catch": [
    {
      "ErrorEquals": ["States.ALL"],
      "Next": "Handle_Failure",
      "Output": "{% $merge([$states.input, {'error': $states.error}]) %}"
    }
  ]
}
```

### Retry Pattern

```json
{
  "Type": "Task",
  "Resource": "...",
  "Retry": [
    {
      "ErrorEquals": ["States.TaskFailed"],
      "IntervalSeconds": 60,
      "MaxAttempts": 3,
      "BackoffRate": 2
    }
  ]
}
```

---

## Timeout Configuration

```json
{
  "Type": "Task",
  "Resource": "...",
  "TimeoutSeconds": 3600,
  "HeartbeatSeconds": 60
}
```

---

## Callback Pattern (waitForTaskToken)

Used for dependencies:

```json
{
  "Type": "Task",
  "Resource": "arn:aws:states:::states:startExecution.waitForTaskToken",
  "Arguments": {
    "Input": {
      "token": "{% $states.context.Task.Token %}"
    }
  }
}
```

Woken by Lambda:
```python
sfn.send_task_success(
    taskToken=token,
    output=json.dumps({"signal": "pass"})
)
```

---

## Best Practices

1. **Use JSONata** - More powerful than JSONPath
2. **Catch all errors** - Prevent stuck executions
3. **Set timeouts** - Default is 1 year!
4. **Use deterministic names** - Prevent duplicates
5. **Emit events** - Enable monitoring
6. **Truncate errors** - DynamoDB has size limits
