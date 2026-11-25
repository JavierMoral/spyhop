{#- The prompt of a turn of a headless agent (`claude -p`). Rendered as a
    single line. turn: "implement" (after the plan is approved), "full"
    (without a plan review), "change" (a change of the person after a
    completed run), "code_review" (the code_review phase, with command) or
    "continue" (after a turn that stopped without a valid end, with the
    problem in changes). new_session: the turn starts a new session after
    the session of the agent is gone. -#}
{%- if new_session -%}
Otro agente trabajó antes en {{ issue_key }} en esta rama y su sesión ya no existe: lee CLAUDE-TASK.md, CLAUDE-PLAN.md si existe, CLAUDE-RESULT.md si existe y los commits de la rama.
{% endif -%}
{%- if turn == "implement" -%}
/goal Lee CLAUDE-TASK.md, vuelve a leer los comentarios de {{ issue_key }} en JIRA e implementa CLAUDE-PLAN.md hasta tener commits locales limpios y los tests del módulo en verde. Escribe CLAUDE-RESULT.md según CLAUDE-TASK.md.
{%- elif turn == "full" -%}
/goal Lee CLAUDE-TASK.md, obtén de JIRA el contexto que indica, analiza el código, escribe CLAUDE-PLAN.md con el enfoque, los tests previstos y los casos similares del código que sigue, según la sección Patterns, e impleméntalo hasta tener commits locales limpios y los tests del módulo en verde. Escribe CLAUDE-RESULT.md según CLAUDE-TASK.md.
{%- elif turn == "change" -%}
/goal Haz este cambio que pide la persona en {{ issue_key }}: {{ changes }}
Haz commits locales limpios y deja en verde los tests de los módulos que cambies. Reescribe CLAUDE-RESULT.md según CLAUDE-TASK.md.
{%- elif turn == "code_review" -%}
/goal Ejecuta `{{ command }}` sobre los commits de esta rama. Deja que aplique los hallazgos más importantes y vuelve a pasar los tests de los módulos que cambie. Después reescribe CLAUDE-RESULT.md según CLAUDE-TASK.md, con los hallazgos de la revisión que se aplicaron y los que se descartaron, y por qué.
{%- else -%}
Continúa con la tarea de {{ issue_key }}. Tu turno anterior terminó así: {{ changes }}
Corrige eso y termina el trabajo que quedaba.
{%- endif %}
El worktree es {{ root }}: trabaja solo en él. No hagas push.
Termina el turno con la salida estructurada: status done y un resumen cuando el trabajo esté hecho, con los commits hechos y CLAUDE-RESULT.md escrito; o status question con la pregunta, cuando necesites que una persona decida algo antes de seguir.
