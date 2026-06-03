import cv2
import mediapipe as mp
import serial
import time
import json
import sqlite3
from datetime import datetime
from collections import deque
from mediapipe.tasks import python
from mediapipe.tasks.python import vision


class AstronautHUD:
    """
    Sistema integrado de detecção de fadiga e HUD de telemetria.

    Encapsula toda a lógica do pipeline:
    inicialização de hardware → captura → inferência → alarme → render → cleanup.
    """

    def __init__(self, port=None, model_path="./face_landmarker.task"):
        # Telemetria & Métricas
        self.fps_queue = deque(maxlen=30)   
        self.total_frames = 0
        self.detected_frames = 0
        self.latency_ms = 0.0
        self.camera_status = "INITIALIZING"

        # Configuração de Sonolência
        self.EAR_THRESH = 0.25        
        self.FRAMES_TO_ALARM = 15    
        self.fatigue_counter = 0
        self.alarm_active = False

        # Inicializa subsistemas
        self._init_db()
        self.arduino = self._init_serial(port)
        self.detector = self._init_ai(model_path)
        self.cap = self._init_camera()

    # INICIALIZAÇÃO DOS SUBSISTEMAS

    def _init_db(self):
        """
        Cria o banco SQLite e a tabela de logs de fadiga se não existirem.
        """
        try:
            self.db_conn = sqlite3.connect(
                "./db/setup.db", check_same_thread=False
            )
            self.cursor = self.db_conn.cursor()
            self.cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS logs_fadiga (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp     TEXT    NOT NULL,
                    evento        TEXT    NOT NULL,
                    ear_registado REAL    NOT NULL
                )
                """
            )
            self.db_conn.commit()
            print("[INFO] Base de dados SQLite inicializada com sucesso.")
        except sqlite3.Error as e:
            print(f"[WARN] Falha ao inicializar base de dados: {e}. Logging desativado.")
            self.db_conn = None
            self.cursor = None

    def _init_serial(self, port):
        """
        Tenta conectar ao Arduino via porta serial.
        Retorna None (modo simulação) se port for None ou se a conexão falhar.
        """
        if not port:
            print("[INFO] Porta serial não configurada. Modo simulação ativo.")
            return None
        try:
            conn = serial.Serial(port, 9600, timeout=1)
            time.sleep(2)   
            print(f"[INFO] Arduino conectado em {port}.")
            return conn
        except serial.SerialException as e:
            print(f"[WARN] Falha na conexão serial ({e}). Modo simulação ativo.")
            return None

    def _init_ai(self, model_path):
        """
        Inicializa o MediaPipe FaceLandmarker a partir do arquivo .task.
        Lança RuntimeError se o modelo não puder ser carregado.
        """
        try:
            base_options = python.BaseOptions(model_asset_path=model_path)
            options = vision.FaceLandmarkerOptions(
                base_options=base_options,
                num_faces=1,
            )
            detector = vision.FaceLandmarker.create_from_options(options)
            print("[INFO] Modelo MediaPipe FaceLandmarker carregado.")
            return detector
        except Exception as e:
            raise RuntimeError(f"Falha crítica ao carregar modelo de IA: {e}")

    def _init_camera(self, src=0):
        """
        Abre o dispositivo de captura e configura resolução 1280×720.
        Lança IOError se a câmera não estiver disponível.
        """
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            self.camera_status = "OFFLINE"
            raise IOError("CRITICAL: Câmera não disponível. Verifique o hardware.")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.camera_status = "ONLINE"
        print("[INFO] Câmera inicializada (1280×720).")
        return cap

    # LÓGICA DE CAPTURA

    def _safe_read(self):
        """
        Leitura robusta de frame com detecção de drop e reconexão automática.

        Retorna o frame (ndarray) em caso de sucesso, ou None se o stream
        estiver em recuperação (o loop principal deve pular o ciclo).
        """
        try:
            ret, frame = self.cap.read()
            if not ret:
                # Frame drop: força reconexão sem travar o loop.
                raise ValueError("Frame drop detectado.")
            return frame
        except Exception as e:
            self.camera_status = "RECOVERING"
            print(f"[ERROR] Stream interrompido: {e}. Tentando reconexão...")
            if self.cap.isOpened():
                self.cap.release()
            time.sleep(1)
            self.cap = cv2.VideoCapture(0)
            if self.cap.isOpened():
                self.camera_status = "ONLINE"
                print("[INFO] Câmera reconectada com sucesso.")
            else:
                self.camera_status = "OFFLINE"
                print("[ERROR] Reconexão falhou. Câmera permanece offline.")
            return None

    # CÁLCULO EAR

    def _calculate_ear(self, eye_landmarks):
        """
        Calcula o Eye Aspect Ratio (EAR) para um conjunto de 6 landmarks oculares.

        Fórmula: EAR = (p2-p6 + p3-p5) / (2 * p1-p4)

        Retorna 0.0 se a distância horizontal for zero (evita divisão por zero).
        """
        v1 = abs(eye_landmarks[1].y - eye_landmarks[5].y)   # distância vertical superior
        v2 = abs(eye_landmarks[2].y - eye_landmarks[4].y)   # distância vertical inferior
        h  = abs(eye_landmarks[0].x - eye_landmarks[3].x)   # distância horizontal
        return (v1 + v2) / (2.0 * h) if h != 0 else 0.0

    # PRÉ-PROCESSAMENTO DE IMAGEM 

    def _preprocess_frame(self, frame):
        """
        Aplica equalização adaptativa de histograma (CLAHE) no canal de luminância
        para normalizar variações de iluminação antes da inferência.

        Pipeline: BGR → LAB → CLAHE no canal L → LAB → BGR
        """
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l_channel, a, b = cv2.split(lab)
        # CLAHE com clipLimit para não saturar detalhes faciais.
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l_channel = clahe.apply(l_channel)
        lab = cv2.merge((l_channel, a, b))
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    # ALARME E LOGGING

    def _trigger_alarm(self, state, current_ear):
        """
        Ativa ou desativa o alarme apenas quando o estado muda (edge-triggered).
        Registra o evento no SQLite e envia o comando JSON ao Arduino.
        """
        if self.alarm_active == state:
            return  

        self.alarm_active = state

        # Log persistente apenas na transição de estado.
        if state:
            self._salvar_log("ALARME_ATIVADO: Fadiga Detetada", current_ear)
        else:
            self._salvar_log("ALARME_DESATIVADO: Retorno ao Estado Nominal", current_ear)

        # Comunicação com Arduino via JSON.
        if self.arduino:
            try:
                payload = {"dir": "A" if state else "N"}
                self.arduino.write(json.dumps(payload).encode() + b"\n")
            except serial.SerialException as e:
                print(f"[ERROR] Falha na transmissão serial: {e}")

    def _salvar_log(self, evento, ear):
        """
        Insere um registro de evento de fadiga no banco SQLite.
        """
        if self.cursor is None or self.db_conn is None:
            return
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.cursor.execute(
                "INSERT INTO logs_fadiga (timestamp, evento, ear_registado) VALUES (?, ?, ?)",
                (timestamp, evento, round(ear, 3)),
            )
            self.db_conn.commit()
            print(f"[LOG] {timestamp} — {evento} (EAR: {ear:.3f})")
        except sqlite3.Error as e:
            print(f"[ERROR] Erro ao salvar log: {e}")

    # RENDERIZAÇÃO DO HUD

    def _draw_hud(self, frame, avg_ear, face_found):
        """
        Renderiza o HUD de telemetria sobre o frame:
        — Card esquerdo: métricas de performance (FPS, latência, taxa de sucesso)
        — Card direito: biometria do piloto (face lock, EAR, status)
        — Barra de progresso central: nível de fadiga acumulado
        — Alerta crítico: borda vermelha e banner quando alarme ativo
        """
        h, w, _ = frame.shape
        overlay = frame.copy()

        # Paleta de cores (BGR)
        neon_blue  = (255, 180, 50)
        neon_green = (100, 255, 100)
        neon_red   = (50, 50, 255)
        white      = (240, 240, 240)

        # 1. Cards de fundo semi-transparente
        cv2.rectangle(overlay, (20, 20),        (280, 230),      (30, 20, 10), -1)  # esquerdo
        cv2.rectangle(overlay, (w - 280, 20),   (w - 20, 200),   (30, 20, 10), -1)  # direito
        frame = cv2.addWeighted(overlay, 0.4, frame, 0.6, 0)

        # Bordas dos cards
        cv2.rectangle(frame, (20, 20),       (280, 230),    neon_blue, 1)
        cv2.rectangle(frame, (w - 280, 20),  (w - 20, 200), neon_blue, 1)

        # 2. Card Esquerdo — Métricas de Performance
        fps_realtime = self.fps_queue[-1] if self.fps_queue else 0.0
        fps_avg      = sum(self.fps_queue) / max(1, len(self.fps_queue))
        success_rate = (self.detected_frames / max(1, self.total_frames)) * 100

        cv2.putText(frame, "METRICS PANEL",                        (35, 45),  cv2.FONT_HERSHEY_DUPLEX,  0.50, neon_blue,  1)
        cv2.putText(frame, f"CAM STATUS : {self.camera_status}",   (35, 85),  cv2.FONT_HERSHEY_SIMPLEX, 0.45, neon_green if self.camera_status == "ONLINE" else neon_red, 1)
        cv2.putText(frame, f"FPS REALTI.: {fps_realtime:.1f}",     (35, 115), cv2.FONT_HERSHEY_SIMPLEX, 0.45, white, 1)
        cv2.putText(frame, f"FPS AVERAGE: {fps_avg:.1f}",          (35, 145), cv2.FONT_HERSHEY_SIMPLEX, 0.45, white, 1)
        cv2.putText(frame, f"LATENCY    : {self.latency_ms:.1f} ms",(35, 175), cv2.FONT_HERSHEY_SIMPLEX, 0.45, white, 1)
        cv2.putText(frame, f"SUCCESS RT : {success_rate:.1f}%",    (35, 205), cv2.FONT_HERSHEY_SIMPLEX, 0.45, white, 1)

        # 3. Card Direito — Biometria do Piloto
        x_r = w - 265
        status_text  = "FATIGUE CRITICAL" if self.alarm_active else "NOMINAL"
        status_color = neon_red if self.alarm_active else neon_green

        cv2.putText(frame, "PILOT BIOMETRICS",                              (x_r, 45),  cv2.FONT_HERSHEY_DUPLEX,  0.50, neon_blue,  1)
        cv2.putText(frame, f"FACE LOCK : {'YES' if face_found else 'NO'}", (x_r, 85),  cv2.FONT_HERSHEY_SIMPLEX, 0.45, neon_green if face_found else neon_red, 1)
        cv2.putText(frame, f"EYE EAR   : {avg_ear:.3f}",                   (x_r, 115), cv2.FONT_HERSHEY_SIMPLEX, 0.45, white, 1)
        cv2.putText(frame, f"STATUS    : {status_text}",                   (x_r, 155), cv2.FONT_HERSHEY_SIMPLEX, 0.45, status_color, 2)

        # 4. Barra de Progresso de Fadiga
        bar_x1, bar_y1 = w // 2 - 150, h - 50
        bar_x2, bar_y2 = w // 2 + 150, h - 30
        progress_pct = min(self.fatigue_counter / self.FRAMES_TO_ALARM, 1.0)
        progress_w   = int((bar_x2 - bar_x1) * progress_pct)
        bar_color    = neon_red if progress_pct > 0.6 else neon_blue

        cv2.rectangle(frame, (bar_x1, bar_y1), (bar_x2, bar_y2), (50, 50, 50), -1)  # fundo
        if progress_w > 0:
            cv2.rectangle(frame, (bar_x1, bar_y1), (bar_x1 + progress_w, bar_y2), bar_color, -1)
        cv2.rectangle(frame, (bar_x1, bar_y1), (bar_x2, bar_y2), white, 1)          # borda
        cv2.putText(frame, f"FATIGUE LEVEL: {int(progress_pct * 100)}%",
                    (bar_x1, bar_y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.4, white, 1)

        # 5. Alerta Crítico
        if self.alarm_active:
            cv2.rectangle(frame, (0, 0), (w, h), neon_red, 8)
            cv2.rectangle(frame, (w // 2 - 200, 70), (w // 2 + 200, 120), (0, 0, 150), -1)
            cv2.putText(frame, "WAKE UP PROTOCOL ACTIVE",
                        (w // 2 - 165, 102), cv2.FONT_HERSHEY_DUPLEX, 0.6, white, 2)

        return frame

    # LOOP PRINCIPAL

    def run(self):
        """Executa o loop de captura, inferência e renderização."""
        print("[INFO] Sistema Online. Pressione 'Q' para encerrar.")
        prev_frame_time = time.time()

        while True:
            cycle_start      = time.time()
            self.total_frames += 1

            # Captura robusta com reconexão automática em caso de frame drop
            frame = self._safe_read()
            if frame is None:
                continue  # câmera em recuperação; aguarda próximo ciclo

            frame = cv2.flip(frame, 1)   # espelha horizontalmente
            h, w, _ = frame.shape

            processed_frame = self._preprocess_frame(frame)

            # Inferência MediaPipe
            rgb_frame = cv2.cvtColor(processed_frame, cv2.COLOR_BGR2RGB)
            mp_image  = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
            result    = self.detector.detect(mp_image)

            avg_ear    = 0.0
            face_found = bool(result.face_landmarks)

            if face_found:
                self.detected_frames += 1
                face = result.face_landmarks[0]

                # Índices dos landmarks oculares no mapa de 468 pontos do MediaPipe.
                right_eye_idx = [33, 160, 158, 133, 153, 144]
                left_eye_idx  = [362, 385, 387, 263, 373, 380]

                ear_r   = self._calculate_ear([face[i] for i in right_eye_idx])
                ear_l   = self._calculate_ear([face[i] for i in left_eye_idx])
                avg_ear = (ear_l + ear_r) / 2.0

                # Desenha os landmarks oculares sobre o frame original (não processado).
                for idx in right_eye_idx + left_eye_idx:
                    pt = face[idx]
                    cv2.circle(frame, (int(pt.x * w), int(pt.y * h)), 2, (0, 255, 255), -1)

                # Lógica de Fadiga
                if avg_ear < self.EAR_THRESH:
                    self.fatigue_counter += 1
                    if self.fatigue_counter >= self.FRAMES_TO_ALARM:
                        self._trigger_alarm(True, avg_ear)
                else:
                    self.fatigue_counter = 0
                    self._trigger_alarm(False, avg_ear)
            else:
                # Sem face detectada: reseta contador e cancela alarme ativo.
                self.fatigue_counter = 0
                self._trigger_alarm(False, avg_ear)

            # Métricas de Performance
            curr_time = time.time()
            self.fps_queue.append(1 / max(curr_time - prev_frame_time, 0.001))
            prev_frame_time  = curr_time
            self.latency_ms  = (time.time() - cycle_start) * 1000

            # Renderização
            frame = self._draw_hud(frame, avg_ear, face_found)
            cv2.imshow("Astronaut Telemetry System", frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        self.cleanup()

    # ENCERRAMENTO SEGURO

    def cleanup(self):
        """
        Libera todos os recursos na ordem correta:
        alarme → câmera → serial → banco de dados → janelas OpenCV.
        """
        print("[INFO] Encerrando sistemas...")

        # Garante que o Arduino recebe o comando de desligamento antes de fechar a porta.
        self._trigger_alarm(False, 0.0)

        if hasattr(self, "cap") and self.cap.isOpened():
            self.cap.release()

        if self.arduino and self.arduino.is_open:
            self.arduino.close()
            print("[INFO] Porta serial fechada.")

        if hasattr(self, "db_conn") and self.db_conn:
            self.db_conn.close()
            print("[INFO] Conexão com SQLite encerrada.")

        cv2.destroyAllWindows()
        print("[INFO] Sistema encerrado.")


# ENTRY POINT

if __name__ == "__main__":
    # Altere port para a porta serial do Arduino (ex: "COM3")
    system = AstronautHUD(port=None)
    system.run()
