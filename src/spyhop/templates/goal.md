{#- The /goal sent as the launch prompt. Rendered as a single line.
    phase: "plan" (plan_review: true), "replan" (a new agent that changes
    CLAUDE-PLAN.md with the changes of the person), "implement" (the launch
    prompt of a new agent, after the plan is approved), "change" (a new
    agent that makes the change of the person after a completed run) or
    "full" (plan_review: false).
    The Review section of CLAUDE-TASK.md, when there is one, runs after the
    implementation and before CLAUDE-RESULT.md. -#}
{%- if phase == "plan" -%}
/goal Lee CLAUDE-TASK.md, obtén de JIRA el contexto que indica, analiza el código y escribe CLAUDE-PLAN.md con el enfoque, los ficheros
afectados, los tests previstos y los casos similares del código que sigue, según la sección Patterns. No modifiques código. Al terminar ejecuta:
{{ spyhop }} declare {{ issue_key }} plan
{%- elif phase == "replan" -%}
/goal Lee CLAUDE-TASK.md y CLAUDE-PLAN.md, y cambia CLAUDE-PLAN.md con estos cambios de la persona que revisa el plan: {{ changes }}
No modifiques código. Al terminar ejecuta:
{{ spyhop }} declare {{ issue_key }} plan
{%- elif phase == "implement" -%}
/goal Lee CLAUDE-TASK.md, vuelve a leer los comentarios de {{ issue_key }} en JIRA e implementa CLAUDE-PLAN.md hasta tener commits locales limpios y los tests del módulo
en verde. Después sigue la sección Review de CLAUDE-TASK.md, si la tiene. Escribe CLAUDE-RESULT.md según CLAUDE-TASK.md y ejecuta:
{{ spyhop }} declare {{ issue_key }} done
{%- elif phase == "change" -%}
/goal Lee CLAUDE-TASK.md y CLAUDE-RESULT.md: otro agente ha implementado {{ issue_key }} en los commits de esta rama. Haz este cambio que pide la persona: {{ changes }}
Haz commits locales limpios y deja en verde los tests de los módulos que cambies. No hagas push. Reescribe CLAUDE-RESULT.md según CLAUDE-TASK.md y ejecuta:
{{ spyhop }} declare {{ issue_key }} done
{%- else -%}
/goal Lee CLAUDE-TASK.md, obtén de JIRA el contexto que indica, analiza el código, escribe CLAUDE-PLAN.md con el enfoque, los tests
previstos y los casos similares del código que sigue, según la sección Patterns, e impleméntalo hasta tener commits locales limpios y los tests del módulo
en verde. Después sigue la sección Review de CLAUDE-TASK.md, si la tiene. Escribe CLAUDE-RESULT.md según CLAUDE-TASK.md y ejecuta:
{{ spyhop }} declare {{ issue_key }} done
{%- endif -%}
