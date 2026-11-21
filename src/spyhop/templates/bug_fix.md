Corrección de un bug:

- Reproduce el fallo antes de cambiar nada.
- En CLAUDE-PLAN.md, escribe de 3 a 5 hipótesis sobre la causa que se puedan
  refutar. Para cada una, indica la prueba que la confirma o la descarta: una
  ejecución, un test o una ruta de código citada con fichero y línea.
- Explica la causa raíz con la evidencia de la hipótesis confirmada. Si dos
  correcciones con la misma premisa fallan, comprueba la premisa antes de
  intentar una tercera.
- Indica en el plan dónde va el test de regresión: la clase de test y el punto
  del código que ejerce.
- Añade un test que falle sin la corrección y pase con ella.
- Haz el cambio mínimo que corrige la causa, no solo el síntoma.
