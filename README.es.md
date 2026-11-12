# SQS + Lambda: rompe el flujo, recupera el mensaje

Provoca un fallo en Lambda, sigue los reintentos de SQS y rescata el mensaje de la DLQ. Nuestro primer lab local: código real, logs reales y sin cuenta de AWS.

## Preparación

- Ruta nativa: un sistema Linux compatible (x86_64 o aarch64, glibc ≥ 2.17), Python 3.9 o superior con venv/pip y un editor de texto. También sirve una VM Linux; esta ruta no requiere Docker.
- Instala AWS Emulator con las instrucciones oficiales para Linux que aparecen abajo. No hay binario nativo del emulador para macOS o Windows; utiliza un entorno Linux compatible o la alternativa con Docker.
- La ruta nativa usa dos terminales y el puerto local 19397. Mantén el emulador encendido durante la práctica. No necesitas cuenta de AWS, credenciales personales ni AWS CLI.
- Ejecución de referencia: AWS Emulator 1.0.322 sobre Linux aarch64, validado dentro de un contenedor desde macOS. La instalación nativa en un host Linux separado no se probó. Instalar paquetes y descargar dependencias requiere internet.

### 1. Clona el repositorio

```bash
git clone https://github.com/leodc/sqs-lambda-retries.git
cd sqs-lambda-retries
```

### 2. Linux, sin Docker · terminal A

Primero instala AWS Emulator para tu arquitectura siguiendo las instrucciones oficiales del enlace. Después prepara Python y arranca el emulador. Deja esta terminal abierta. Si tu distribución separa python3-venv o pip en otro paquete, instálalo primero. Espera a que el emulador indique que está listo.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
aws-emulator --version
bash start-emulator.sh
```

https://docs.aws.amazon.com/emulator/latest/userguide/getting-started-linux.html

### 3. Linux · terminal B

En otra terminal, entra en la carpeta del repositorio clonado (ajusta la ruta si hace falta) y activa su entorno de Python. Ejecuta aquí las etapas de la práctica.

```bash
cd sqs-lambda-retries
source .venv/bin/activate
```

### Alternativa para macOS / Windows: Docker

Usa esta opción en lugar de los dos pasos de terminal Linux. Desde la carpeta del repositorio clonado, construye la imagen Linux y entra en su shell. El emulador arranca automáticamente; incluye Python y nano. Ejecuta todas las etapas dentro de esa shell. El contenedor en ejecución no tiene red externa, credenciales AWS montadas ni puertos publicados. Construir la imagen requiere internet y un motor Docker configurado para contenedores Linux.

```bash
docker build -t sqs-lambda-practice .
docker run --rm -it --name sqs-lambda-practice --network none --memory=2g --memory-swap=2g sqs-lambda-practice
```

## 1. Prepara el experimento con el consumo desactivado

Crea una cola origen, su dead-letter queue, una función Lambda y un event source mapping desactivado. El script usa únicamente las credenciales generadas por el emulador y un endpoint local explícito.

```bash
python lab.py prepare
python lab.py inspect
```

- Busca el mapping en Disabled, consumer fail_poison en true y las dos colas vacías.
- Lee configuration: batch size 1, timeout de función de 1 segundo, visibility timeout de 6 segundos y max_receive_count 3. Abre handler.py antes de avanzar.

¿Por qué comenzar con el mapping desactivado?

Te da un punto estable para inspeccionar la infraestructura antes de que Lambda consulte la cola. Todavía no se ha enviado ningún mensaje.
Los valores cortos facilitan observar el fallo. AWS recomienda un visibility timeout de al menos seis veces el timeout de la función y maxReceiveCount de al menos cinco; tres es una elección didáctica, no una recomendación para producción.

## 2. Envía dos mensajes y detente

Envía order-ok y order-poison. Enviar un mensaje no debe iniciar la siguiente etapa sin que lo decidas.

```bash
python lab.py send
python lab.py inspect
```

- Busca dos registros sent con IDs diferentes. Guarda el ID de order-poison.
- La cola origen debe tener dos mensajes visibles; el mapping sigue en Disabled y la DLQ está vacía.

¿Por qué todavía no hay logs del handler?

Un mensaje en SQS no invoca por sí solo la función. El event source mapping activado consulta SQS e invoca Lambda. Lo hemos dejado desactivado a propósito.
Una segunda llamada a send se rechaza en esta sesión: copiar dos veces el bloque no añade otra pareja de mensajes.

## 3. Activa el consumo y observa el fallo

Inicia el polling. Desde aquí los reintentos avanzan en segundo plano, incluso mientras lees. Puedes repetir inspect en cualquier momento para recuperar logs nuevos.

```bash
python lab.py consume
python lab.py inspect
```

- Busca received y processed para order-ok, y deliberate_failure para order-poison. Si aún no llegan los logs, repite inspect.
- Sigue message_id y receive_count. Los contadores cambian mientras trabaja el consumidor; cero mensajes visibles no demuestra éxito.

¿Qué permite que el mensaje fallido vuelva a estar disponible?

El handler lanza un error para order-poison. El mensaje no se confirma y elimina como procesado correctamente. Cuando vence su visibility timeout puede recibirse otra vez.
Con batch size uno, el mensaje válido no se reintenta junto al fallido. Los lotes mayores y las respuestas parciales son otro experimento.

## 4. Inspecciona los reintentos y la DLQ

Espera a que el mensaje fallido llegue a la DLQ e inspecciona su contenido y metadatos. El comando tiene una espera limitada; no repara el consumidor ni devuelve el mensaje a la cola origen.

```bash
python lab.py diagnose
python lab.py inspect
```

- Busca tres logs deliberate_failure del mismo mensaje, con receive_count 1, 2 y 3. Son tres entregas: el intento inicial y dos reintentos.
- Busca un evento diagnosis para order-poison, el log de éxito de order-ok y un mensaje visible en la DLQ.
- Para inspeccionarlo, el script recibe el mensaje de la DLQ y restablece inmediatamente su visibilidad. Esa lectura también incrementa el contador de recepciones.

¿Un receive count de 4 demuestra que Lambda falló cuatro veces?

No. En la ejecución de referencia Lambda falló tres veces. La cuarta recepción fue nuestra lectura de diagnóstico en la DLQ, no otra invocación de la función. Cuenta los logs reales de fallo del handler.
La política de redrive de la cola origen mueve el mensaje a su DLQ. Es una configuración de SQS, distinta de la DLQ para invocaciones asíncronas de Lambda.
Si repites diagnose, devuelve el diagnóstico guardado sin volver a recibir el mensaje de la DLQ.

## 5. Corrige el consumidor y vuelve a inspeccionar

Abre consumer.json y cambia fail_poison de true a false. En nano, guarda con Ctrl+O, Enter y sal con Ctrl+X. Después aplica el cambio de configuración a la función emulada.

```bash
nano consumer.json
python lab.py repair
python lab.py inspect
```

- Confirma consumer fail_poison en false. Editar el archivo no actualiza la función: repair aplica el cambio.
- La DLQ debe seguir con un mensaje. Corregir la función no lo ha devuelto a la cola origen.

¿Por qué la función corregida todavía no ha procesado ese mensaje?

El mapping consulta la cola origen. El mensaje está esperando en otra cola: la DLQ. Corregir el consumidor y recuperar los mensajes fallidos son operaciones distintas.
El script rechaza repair mientras fail_poison siga en true. Editar el archivo es una parte explícita de la práctica.

## 6. Devuelve el mensaje a la cola origen

Inicia una tarea de redrive desde la DLQ hacia la cola origen después de inspeccionar la corrección. El consumidor sigue activado.

```bash
python lab.py redrive
python lab.py inspect
```

- Busca redrive_started con un task handle. Confirma la solicitud, todavía no la recuperación completa.
- El mensaje puede estar procesándose cuando inspecciones. En la siguiente etapa comprobarás la tarea terminada, el éxito del handler y ambas colas.

¿Qué pasaría si hicieras redrive antes de corregir el fallo?

El mensaje volvería a encontrarse con el mismo handler defectuoso. Redrive mueve mensajes; no resuelve la causa del fallo.
El orden de etapas del lab evita ese error. Una recuperación en producción necesita el mismo razonamiento aunque la API permita hacerlo.

## 7. Verifica algo más que una cola vacía

Espera a que termine la tarea de redrive, aparezca processed para order-poison y ambas colas tengan cero mensajes visibles, en vuelo y retrasados.

```bash
python lab.py verify
python lab.py inspect
```

- Busca verification con result PASS y el evento processed de order-poison.
- Lee redrive_identity_observed. En el emulador 1.0.322 se conservaron el ID y el contador. AWS documenta nuevos IDs y horas de encolado después de redrive: es una diferencia observada del emulador.

¿Por qué no basta con ver la DLQ vacía?

El mensaje podría haberse movido sin procesarse correctamente. El script combina tarea completada, un log de éxito del handler y contadores de las colas.
Usa el emulador para estudiar el flujo, pero no asumas que conservar el ID o los tiempos exactos de reintento representan el comportamiento de AWS.

## 8. Conserva la evidencia y limpia

Tus observaciones locales quedan en .lab/events.jsonl, incluidos los mensajes originales de los logs de Lambda. Revísalas y guárdalas antes de detener el entorno. Cleanup elimina los recursos emulados de esta sesión y conserva los archivos de evidencia.

```bash
python lab.py cleanup
tail -n 5 .lab/events.jsonl
```

- Busca cleanup_completed. Se elimina el estado guardado de los recursos; .lab/events.jsonl se conserva.
- Linux nativo: detén la terminal A con Ctrl+C. Docker: antes de salir, copia .lab desde una terminal del host con docker cp sqs-lambda-practice:/opt/lab/.lab ./practice-logs. Después sal de la shell del contenedor.
- Si reiniciaste el emulador a mitad de la práctica, sus recursos en memoria ya no existen. Ejecuta python lab.py reset y luego prepare para empezar otra vez. Los logs se conservan: usa las fechas para separar intentos.

¿Qué evidencia usarías para explicarle este incidente a otra persona?

Conserva el ID original, las tres entradas de fallo, la lectura de diagnóstico de la DLQ, la corrección aplicada, la tarea de redrive y el éxito final. Explican más que una captura de una cola vacía.
Revisa el log de arranque del emulador antes de publicarlo: puede contener credenciales locales generadas. El archivo events no incluye esas credenciales.

## Límites

- Es ejecución real contra un emulador, no dentro de una cuenta de AWS. No demuestra el enforcement de IAM, las cuotas, el escalado, la durabilidad ni los tiempos de producción.
- El runtime de referencia es 1.0.322. Una nueva versión puede cambiar el comportamiento; el script registra la versión y los hashes del código en el evento environment.
- La práctica usa una cola estándar, un registro por invocación y un fallo determinista. No prueba orden FIFO, efectos duplicados ni respuestas parciales de un batch.
- Un terraform plan revisa cambios propuestos de infraestructura. Esta práctica observa procesamiento de mensajes y recuperación de fallos. Responden preguntas distintas.
