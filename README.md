# 🛰️ Astronaut Telemetry System — Detector de Sonolência em Tempo Real

> **Physical Computing IoT & IoB — Global Solution 2026**  
> Engenharia de Software · FIAP

Sistema embarcado de visão computacional que monitora fadiga ocular em tempo real via webcam, calcula o **Eye Aspect Ratio (EAR)** frame a frame usando MediaPipe Face Landmarker e aciona um alarme físico no Arduino quando detecta sonolência crítica. Todos os eventos são persistidos em banco SQLite local para auditoria posterior.

---

## 👥 Integrantes do Grupo

| Nome Completo | RM |
|---|---|
| Arthur Bobadilla Franchi | RM 555056 |
| Luan Orlandelli Ramos | RM 554747 |
| Jorge Luiz | RM 554418 |

> **Turma:** [3ESPZ] · **Ano:** 2026

---

## 📋 Índice

1. [Objetivos](#-objetivos)
2. [Diagrama da Solução](#-diagrama-da-solução)
3. [Pipeline de Visão Computacional](#-pipeline-de-visão-computacional)
4. [Stack Tecnológica](#-stack-tecnológica)
5. [Estrutura do Repositório](#-estrutura-do-repositório)
6. [Setup e Execução](#-setup-e-execução)
7. [Funcionamento do Hardware](#-funcionamento-do-hardware)
8. [Parâmetros de Calibração](#-parâmetros-de-calibração)
9. [Banco de Dados](#-banco-de-dados)

---

## 🎯 Objetivos

### Objetivo de Negócio

Missões de manutenção em satélites em órbita exigem concentração absoluta do astronauta durante horas de atividade extravehicular (EVA). Um episódio de sonolência nesse contexto pode resultar em falha de procedimento, dano ao satélite ou risco à vida do operador, sem possibilidade de intervenção imediata por parte da equipe em solo.

O sistema propõe acoplar um módulo de visão computacional ao capacete do astronauta, monitorando continuamente os índices oculares via câmera interna. Ao detectar sinais precoces de fadiga, o sistema alerta a central de controle em tempo real via telemetria, permitindo que a equipe em solo tome decisões operacionais, como interromper o procedimento, acionar o astronauta por rádio ou iniciar protocolo de retorno, antes que o estado de sonolência comprometa a missão.

### Objetivo Técnico

Construir um pipeline de inferência em Python com latência inferior a 50 ms por frame, operando em resolução 1280×720, que:

- Detecte landmarks faciais em tempo real via **MediaPipe FaceLandmarker** (modelo `.task`);
- Calcule o **EAR (Eye Aspect Ratio)** de ambos os olhos a cada frame;
- Aplique pré-processamento **CLAHE** para normalizar variações de iluminação antes da inferência;
- Dispare alarme no **Arduino** via protocolo serial JSON ao detectar olhos fechados por ≥ 15 frames consecutivos;
- Exiba um **HUD de telemetria** com FPS em tempo real, FPS médio (janela de 30 frames), latência, taxa de detecção e barra de progresso de fadiga;
- Persista todos os eventos de alarme em **SQLite** com timestamp e valor EAR registrado;
- Tratar falhas de hardware (câmera offline, frame drop) com **reconexão automática** sem travar o loop.

---

## 🗺️ Diagrama da Solução

```
┌─────────────────────────────────────────────────────────────────────┐
│                        LOOP PRINCIPAL (run)                         │
│                                                                     │
│  ┌──────────┐    ┌───────────────┐    ┌──────────────────────────┐  │
│  │  Câmera  │───▶│  _safe_read   │───▶│  _preprocess_frame       │  │
│  │  (CV2)   │    │  (reconexão   │    │  BGR → LAB → CLAHE → BGR │  │
│  │  1280x720│    │   automática) │    │  (normaliz. iluminação)  │  │
│  └──────────┘    └───────────────┘    └──────────────┬───────────┘  │
│                                                      │              │
│                                          ┌───────────▼───────────┐  │
│                                          │  MediaPipe Inferência │  │
│                                          │  FaceLandmarker       │  │
│                                          │  (468 landmarks)      │  │
│                                          └───────────┬───────────┘  │
│                                                      │              │
│                                          ┌───────────▼───────────┐  │
│                                          │  _calculate_ear       │  │
│                                          │  EAR = (v1+v2)/(2*h)  │  │
│                                          │  olho D + olho E → avg│  │
│                                          └───────────┬───────────┘  │
│                                                      │              │
│                         EAR < 0.25?                  │              │
│                    ┌────────────────────────────────▶│              │
│                    │ SIM: fatigue_counter++          │              │
│                    │      counter ≥ 15?              │              │
│                    │      ┌──────────────────────┐   │              │
│                    │      │  _trigger_alarm(True)│   │              │
│                    │      │  → SQLite log        │   │              │
│                    │      │  → Serial JSON {"A"} │   │              │
│                    │      └──────────────────────┘   │              │
│                    │ NÃO: fatigue_counter = 0        │              │
│                    │      _trigger_alarm(False)      │              │
│                    └─────────────────────────────────┘              │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  _draw_hud: FPS realtime │ FPS avg │ Latência │ EAR │ Status │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                               │ Serial (9600 baud, JSON)
                    ┌──────────▼──────────┐
                    │     ARDUINO UNO     │
                    │  {"dir": "A"} →     │
                    │    Buzzer 880Hz Hi  │
                    │    Buzzer 698Hz Lo  │
                    │    LED pino 13 ↑    │
                    │  {"dir": "N"} →     │
                    │    noTone + LED OFF │
                    └─────────────────────┘
```

### Fluxo de Estados da Câmera

```
INITIALIZING ──► ONLINE ──► RECOVERING ──► ONLINE
                              │
                              └──► OFFLINE (reconexão falhou)
```

### Máquina de Estados do Alarme (edge-triggered)

```
NOMINAL ──(EAR < 0.25 por 15 frames)──► FATIGUE CRITICAL
          ◄──(EAR ≥ 0.25 ou sem face)──
```

> O alarme é acionado **apenas na transição de estado** (edge-triggered), evitando flood de mensagens seriais e de logs no banco.

---

## 🔬 Pipeline de Visão Computacional

### 1. Captura e Pré-processamento

O frame bruto capturado em 1280×720 px pela OpenCV passa pelo método `_preprocess_frame` antes de ser enviado à inferência. É aplicada **CLAHE (Contrast Limited Adaptive Histogram Equalization)** exclusivamente no canal **L (luminância)** do espaço de cor **CIE LAB**, preservando os canais cromáticos A e B intocados:

```
BGR → LAB → split(L, A, B) → CLAHE(L, clipLimit=2.0, tile=8×8) → merge → BGR
```

Isso normaliza subexposição, superexposição e iluminação lateral assimétrica sem introduzir artefatos de cor, aumentando a taxa de detecção em condições adversas.

### 2. Detecção de Landmarks — MediaPipe FaceLandmarker

O modelo `face_landmarker.task` (MediaPipe Tasks API) retorna **468 landmarks faciais normalizados** `(x, y, z)` em coordenadas relativas ao frame. A inferência opera em modo **síncrono** (`detect()`), adequado ao loop de captura frame a frame.

### 3. Cálculo do EAR (Eye Aspect Ratio)

Para cada olho, são selecionados 6 landmarks específicos do mapa de 468 pontos:

| Olho | Índices MediaPipe |
|------|-------------------|
| Direito | 33, 160, 158, 133, 153, 144 |
| Esquerdo | 362, 385, 387, 263, 373, 380 |

A fórmula EAR calcula a razão entre a abertura vertical e a abertura horizontal do olho:

```
EAR = (‖p2 − p6‖ + ‖p3 − p5‖) / (2 × ‖p1 − p4‖)
```

Onde `p1`–`p4` são os extremos horizontal e `p2`, `p3`, `p5`, `p6` são os pontos verticais. O EAR médio dos dois olhos é calculado a cada frame. Implementação com proteção contra divisão por zero (`h = 0`).

**Interpretação do EAR:**

| Condição | Valor típico |
|----------|-------------|
| Olhos completamente abertos | 0.30 – 0.45 |
| Piscada natural (< 3 frames) | < 0.25 (transitório) |
| Olhos fechados / sonolência | < 0.25 (persistente) |

### 4. Lógica de Decisão de Fadiga

O contador `fatigue_counter` é incrementado a cada frame com `EAR < EAR_THRESH (0.25)` e zerado ao primeiro frame acima do limiar. O alarme é disparado ao atingir `FRAMES_TO_ALARM = 15` frames consecutivos — o que equivale a aproximadamente **500 ms** a 30 FPS, filtrando piscadas normais.

### 5. Métricas de Performance

O FPS é calculado com uma **janela deslizante de 30 amostras** (`deque(maxlen=30)`), fornecendo FPS instantâneo e FPS médio. A latência de ciclo (`latency_ms`) mede o tempo total entre a captura e a renderização do frame.

---

## 🧰 Stack Tecnológica

| Biblioteca | Versão | Função no Projeto |
|---|---|---|
| `opencv-python` | 4.10.0.84 | Captura de webcam, pré-processamento CLAHE, desenho do HUD e exibição do frame |
| `mediapipe` | 0.10.21 | Face Landmarker: detecção de 468 landmarks faciais e extração dos pontos oculares |
| `pyserial` | 3.5 | Comunicação serial USB/UART com Arduino (envio de payload JSON) |
| `sqlite3` | stdlib | Persistência local de eventos de fadiga com timestamp |
| `json` | stdlib | Serialização do protocolo de comunicação `{"dir": "A"/"N"}` com o Arduino |
| `collections.deque` | stdlib | Janela deslizante de 30 amostras para cálculo de FPS médio |
| `datetime` | stdlib | Geração de timestamps nos registros de telemetria |
| `ArduinoJson` | firmware | Desserialização do JSON recebido pela porta serial no Arduino |

**Requisitos de ambiente:**

- Python **3.13** (versão exigida pela API MediaPipe Tasks)
- Arduino IDE com biblioteca **ArduinoJson** instalada (via Library Manager)
- Webcam USB ou embutida com suporte a resolução mínima 640×480

---

## 📂 Estrutura do Repositório

```
physical_computing_gs01/
├── src
│    ├──main.py                    # Script principal — pipeline completo de VC
│    ├──main.ino                   # Firmware Arduino — sirene Hi-Lo não bloqueante
├── db
│    ├──setup.db
├── face_landmarker.task       # Modelo MediaPipe FaceLandmarker (binário .task)
├── requirements.txt           # Dependências Python com versões fixadas
└── README.md                  # Este arquivo
```

---

## ⚙️ Setup e Execução

### Pré-requisitos

- Python 3.13 instalado e disponível no PATH
- Git instalado
- Arduino IDE (para upload do firmware)

### 1. Clonar o repositório

```bash
git clone https://github.com/aarthurbf/physical_computing_gs01.git
cd physical_computing_gs01
```

### 2. Criar e ativar o ambiente virtual

```bash
# Criar o venv
python -m venv .venv

# Ativar (Linux / macOS)
source .venv/bin/activate

# Ativar (Windows PowerShell)
.venv\Scripts\Activate.ps1

# Ativar (Windows CMD)
.venv\Scripts\activate.bat
```

### 3. Instalar as dependências

```bash
pip install -r requirements.txt
```

> As versões estão fixadas no `requirements.txt` para garantir reprodutibilidade exata do ambiente.

### 4. (Opcional) Configurar o Arduino

Abra `main.ino` na Arduino IDE e carregue para a placa. Instale a biblioteca **ArduinoJson** via _Sketch → Include Library → Manage Libraries_.

Após o upload, identifique a porta serial:

```bash
# Linux
ls /dev/ttyUSB* /dev/ttyACM*

# Windows — verificar no Gerenciador de Dispositivos
# Exemplo: COM3, COM4...
```

No arquivo `main.py`, linha final, altere o parâmetro `port`:

```python
# Com Arduino conectado:
system = AstronautHUD(port="/dev/ttyUSB0")  # Linux
system = AstronautHUD(port="COM3")          # Windows

# Sem Arduino (modo simulação — padrão):
system = AstronautHUD(port=None)
```

### 5. Executar

```bash
python main.py
```

A janela **"Astronaut Telemetry System"** abrirá com o feed da webcam e o HUD de telemetria. Pressione **`Q`** para encerrar com segurança.

### Saída esperada no terminal

```
[INFO] Base de dados SQLite inicializada com sucesso.
[INFO] Porta serial não configurada. Modo simulação ativo.
[INFO] Modelo MediaPipe FaceLandmarker carregado.
[INFO] Câmera inicializada (1280×720).
[INFO] Sistema Online. Pressione 'Q' para encerrar.
```

---

## 🔌 Funcionamento do Hardware

### Esquema de Ligação

| Componente | Pino Arduino |
|---|---|
| Buzzer (polo +) | Pino Digital **9** |
| LED de alerta | Pino Digital **13** (LED interno da placa) |
| GND | GND |

### Protocolo de Comunicação Serial

O Python envia payloads JSON terminados em `\n` a **9600 baud**:

| Evento | Payload enviado | Ação no Arduino |
|---|---|---|
| Fadiga detectada | `{"dir": "A"}` | Liga sirene Hi-Lo (880 Hz / 698 Hz alternados a cada 300 ms) + LED |
| Retorno ao normal | `{"dir": "N"}` | `noTone()` + LED desligado |

O firmware Arduino usa `millis()` para o temporização da sirene, garantindo que o loop nunca bloqueie aguardando `delay()`.

---

## 🎛️ Parâmetros de Calibração

Os dois parâmetros principais estão definidos no `__init__` da classe `AstronautHUD` e podem ser ajustados conforme o usuário:

| Parâmetro | Valor padrão | Efeito |
|---|---|---|
| `EAR_THRESH` | `0.25` | Limiar de fechamento ocular. Diminuir se alarmar com olho aberto; aumentar se não alarmar com olho fechado. |
| `FRAMES_TO_ALARM` | `15` | Frames consecutivos abaixo do limiar para disparar alarme (~500 ms a 30 FPS). Aumentar para reduzir falsos positivos. |

---

## 🗄️ Banco de Dados

Os eventos de fadiga são persistidos automaticamente em `setup.db` (SQLite, criado na primeira execução):

```sql
CREATE TABLE logs_fadiga (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,   -- formato: YYYY-MM-DD HH:MM:SS
    evento        TEXT    NOT NULL,   -- ALARME_ATIVADO / ALARME_DESATIVADO
    ear_registado REAL    NOT NULL    -- valor EAR no momento do evento
);
```

Para consultar os registros:

```bash
sqlite3 setup.db "SELECT * FROM logs_fadiga ORDER BY id DESC LIMIT 20;"
```
