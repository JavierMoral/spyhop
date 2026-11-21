Tarea técnica:

- Mantén el comportamiento observable salvo que el ticket pida cambiarlo.
- Ejecuta los tests de los módulos afectados antes y después del cambio.

### Dependencias

Comprueba las dependencias antes de planificar. Una dependencia que aparece a
mitad de la implementación obliga a planificar de nuevo.

- **Bloqueos declarados.** Si el ticket tiene enlaces `is blocked by` o
  `depends on` a tickets que no están terminados, para. No escribas código.
  Anota los bloqueos en CLAUDE-PLAN.md.
- **Requisitos no declarados.** Busca en el código las clases que piden los
  criterios de aceptación. Si falta la base que tienen que extender y un subtask
  hermano abierto dice en su título que la crea, para. Escribe en CLAUDE-PLAN.md
  las tres opciones y recomienda una:
  - Apilar sobre el hermano: la rama sale de la rama del hermano y se entrega
    después de ella.
  - Seguir de forma independiente: la base se construye aquí y el solape se
    resuelve al integrar.
  - Parar: el hermano tiene que entrar antes.
- No absorbas nunca el trabajo de un subtask hermano para desbloquearte.

Para detenerte, ejecuta el mismo comando `worktree set` que indica el objetivo,
con el comentario «<KEY>: bloqueado, ver CLAUDE-PLAN.md». Explica cada decisión
completa: qué es, el `path:line` del que depende, las opciones y tu
recomendación.

### Tests: rojo antes que verde

Un test que nunca has visto fallar solo prueba que se ejecuta.

- **Tests unitarios:** escribe el test antes del código de producción. Míralo
  fallar por el motivo que indica el criterio. Después implementa hasta que pase.
- **Tests de integración:** ejecútalos antes de desplegar tus cambios. El bundle
  todavía tiene el código antiguo y los tests ya son los nuevos. Clasifica cada
  test nuevo:
  - Falla en su aserción: es el rojo bueno. Anota la aserción.
  - Falla por un símbolo que no existe, un 404 o un error de compilación: es un
    rojo aceptable. Anota el símbolo.
  - Pasa: es un hallazgo, no un éxito. El test no prueba el criterio. Corrígelo
    antes de seguir.
- Si un test existente falla en esa ejecución, es una regresión que ya existía.
  Investígala antes de seguir.
- Cuando el cambio solo mantiene el comportamiento, no hay rojo. Entonces los
  tests existentes deben pasar antes y después del cambio.

### Cómo verificar los tests

- Ejecuta clases de test y módulos completos. Usa `--tests` con un solo método
  solo para depurar un fallo, nunca para la verificación final.
- No leas el resultado del código de salida. `testIntegration` se ejecuta con
  `ignoreFailures`, así que `BUILD SUCCESSFUL` y `rc=0` no prueban nada. Busca
  `> .* FAILED$` en el log y lee `failures` y `errors` en
  `test-results/integration/TEST-*.xml`.
- No pases la salida de Gradle por `tail`, porque oculta el estado del build.
- Si un test falla justo después de desplegar, ejecútalo otra vez antes de
  investigar. Suele ser un estado antiguo del runtime, no un defecto.
- Cada test que falló en rojo debe pasar ahora. Un test en rojo en las dos
  ejecuciones es un fallo real: investígalo y no lo des por esperado.
- No digas que la tarea está terminada con tests en rojo o sin ejecutar. Si una
  suite no se puede ejecutar, nómbrala y explica por qué.
- En CLAUDE-RESULT.md, indica para cada test nuevo cómo falló antes del cambio
  y que pasa después. Copia los números de los ficheros de resultados.
