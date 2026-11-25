{#- The prompt that Spyhop sends to the implementation agent when checkpoints
    after the implementation fail. Rendered as a single line. new_agent: the
    launch prompt of a new agent, when the agent of the run is gone.
    headless: a turn of a headless agent, which ends with its structured
    output, not with `spyhop declare` (see turn.md). -#}
{% if new_agent %}/goal Lee CLAUDE-TASK.md y CLAUDE-RESULT.md: otro agente ha implementado {{ issue_key }} en los commits de esta rama.
{% endif %}Spyhop ha ejecutado los checkpoints y han fallado: {{ failed | join(", ") }}.
Lee CLAUDE-CHECKPOINTS.md y los informes {% for name in failed %}CLAUDE-CHECKPOINT-{{ name }}.md{{ ", " if not loop.last }}{% endfor %}.
Corrige los hallazgos high y medium que sean correctos, con un commit por corrección, y vuelve a pasar los tests de los módulos que cambies.
En un hallazgo de mutation, corrige el test, no el código de producción.
Una corrección de seguridad lleva un título de commit neutro, que dice qué hace el cambio y nunca describe el problema; lo mismo vale para los comentarios y los nombres de test.
Si un hallazgo no es correcto, no cambies el código y explica el motivo en CLAUDE-RESULT.md. Escribe también el motivo en CLAUDE-NOTE-<checkpoint>.md, con los comandos y la salida que lo prueban: Spyhop da esa nota al checkpoint en la ronda siguiente, y el checkpoint la comprueba. Borra la nota de un checkpoint cuando ya no sea correcta.
No edites CLAUDE-CHECKPOINTS.md ni los informes. No hagas push.
{% if headless %}Después reescribe CLAUDE-RESULT.md.{% else %}Después reescribe CLAUDE-RESULT.md y ejecuta:
{{ spyhop }} declare {{ issue_key }} done{% endif %}
