{#- The /goal sent as the launch prompt. Rendered as a single line.
    phase: "plan" (plan_review: true), "implement" (after the plan is approved)
    or "full" (plan_review: false). -#}
{%- if phase == "plan" -%}
/goal Lee TASK.md, analiza el código y escribe PLAN.md con el enfoque, los ficheros
afectados y los tests previstos. No modifiques código. Al terminar ejecuta:
{{ orca }} worktree set --worktree active --workspace-status {{ plan_review_status }}
--comment "{{ issue_key }}: plan listo" --unread --json
{%- elif phase == "implement" -%}
/goal Implementa PLAN.md hasta tener commits locales limpios y los tests del módulo
en verde. Escribe RESULT.md según TASK.md y ejecuta:
{{ orca }} worktree set --worktree active --workspace-status {{ review_status }}
--comment "{{ issue_key }}: listo para revisión" --unread --json
{%- else -%}
/goal Lee TASK.md, analiza el código, escribe PLAN.md con el enfoque y los tests
previstos, e impleméntalo hasta tener commits locales limpios y los tests del módulo
en verde. Escribe RESULT.md según TASK.md y ejecuta:
{{ orca }} worktree set --worktree active --workspace-status {{ review_status }}
--comment "{{ issue_key }}: listo para revisión" --unread --json
{%- endif -%}
