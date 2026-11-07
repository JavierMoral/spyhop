{#- The /goal sent as the launch prompt. Rendered as a single line.
    phase: "plan" (plan_review: true), "implement" (after the plan is approved)
    or "full" (plan_review: false). -#}
{%- if phase == "plan" -%}
/goal Lee CLAUDE-TASK.md, obtén de JIRA el contexto que indica, analiza el código y escribe CLAUDE-PLAN.md con el enfoque, los ficheros
afectados, los tests previstos y los casos similares del código que sigue, según la sección Patterns. No modifiques código. Al terminar ejecuta:
{{ orca }} worktree set --worktree active --workspace-status {{ plan_review_status }}
--comment "{{ issue_key }}: plan listo" --unread --json
{%- elif phase == "implement" -%}
/goal Implementa CLAUDE-PLAN.md hasta tener commits locales limpios y los tests del módulo
en verde. Escribe CLAUDE-RESULT.md según CLAUDE-TASK.md y ejecuta:
{{ orca }} worktree set --worktree active --workspace-status {{ review_status }}
--comment "{{ issue_key }}: listo para revisión" --unread --json
{%- else -%}
/goal Lee CLAUDE-TASK.md, obtén de JIRA el contexto que indica, analiza el código, escribe CLAUDE-PLAN.md con el enfoque, los tests
previstos y los casos similares del código que sigue, según la sección Patterns, e impleméntalo hasta tener commits locales limpios y los tests del módulo
en verde. Escribe CLAUDE-RESULT.md según CLAUDE-TASK.md y ejecuta:
{{ orca }} worktree set --worktree active --workspace-status {{ review_status }}
--comment "{{ issue_key }}: listo para revisión" --unread --json
{%- endif -%}
