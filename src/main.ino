#include <ArduinoJson.h>

const int pinoBuzzer = 9;
const int pinoLED    = 13;

// Variáveis para o alarme não bloqueante
bool alarmeAtivo = false;
unsigned long delayAlarme = 300;
unsigned long anteriorMillis = 0;
bool tomAgudo = true;

void setup() {
  Serial.begin(9600);
  
  pinMode(pinoBuzzer, OUTPUT);
  pinMode(pinoLED, OUTPUT);

  // Beep de inicialização
  tone(pinoBuzzer, 1000, 100);
  Serial.println("Sistema Pronto!");
}

void loop() {
  if (Serial.available() > 0) {
    String entrada = Serial.readStringUntil('\n');

    JsonDocument doc;
    DeserializationError erro = deserializeJson(doc, entrada);

    if (!erro) {
      const char* status = doc["dir"];

      // --- ALARME ---
      if (status[0] == 'A') {
        alarmeAtivo = true;
      } 
      else if (status[0] == 'N') {
        alarmeAtivo = false;
        digitalWrite(pinoLED, LOW);
        noTone(pinoBuzzer);
      }
    }
  }

  // Lógica da sirene (Executa sempre, mas só faz algo se alarmeAtivo for true)
  if (alarmeAtivo) {
    unsigned long atualMillis = millis();

    if (atualMillis - anteriorMillis >= delayAlarme) {
      anteriorMillis = atualMillis;

      if (tomAgudo) {
        digitalWrite(pinoLED, HIGH);
        tone(pinoBuzzer, 880); // Hi
      } else {
        digitalWrite(pinoLED, LOW);
        tone(pinoBuzzer, 698); // Lo
      }
      tomAgudo = !tomAgudo;
    }
  }
}
