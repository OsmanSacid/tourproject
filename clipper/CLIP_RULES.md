# Clip-Regeln (vom Nutzer – gelten für ALLE Agents/Sessions)

Der Nutzer war mit früheren Clips unzufrieden. Jeder Clip muss diese Punkte erfüllen,
sonst wird er NICHT geschickt:

1. **Interesse + Sinn**: Der Clip muss etwas zeigen, das Neugier weckt und für sich allein
   verständlich ist (lustig, verrückt, Zitat, Reaktion). Kein zufälliges Gameplay.
2. **Kontext, nicht mitten im Geschehen**: Start am Anfang der Situation/des Satzes
   (Setup → Höhepunkt → Reaktion). Tool: Standard-Schnitt `quality.context_window`,
   empfohlen `--min-len 20 --max-len 40`.
3. **Kein 18+ / keine Nacktheit**: NudeNet prüft jedes Bild, Sex-Wörter im Transkript
   → Clip wird automatisch verworfen. Zusätzlich jeden Clip selbst ansehen (Kontaktabzug `.jpg`).
4. **Hook-Titel am Anfang**: z.B. „The NPCs in GTA 6 are crazy 💀“. Titel werden vom Agent
   pro Clip geschrieben (nach dem Lesen des Transkripts in `review.json`) und mit
   `--titles titles.json` eingebrannt (weiße Box oben). Kampagnen-Regeln beachten
   (z.B. Name des Streamers im Text, keine Logos).
5. **Facecam richtig + Untertitel immer da**: `--layout auto --facecam x,y,w,h` (vorher Frame
   ausmessen). Clips mit zu wenig Sprache (`--min-words`) werden verworfen. Vor dem Senden den
   Kontaktabzug jedes Clips ansehen: sitzt die Facecam, sind Titel und Untertitel sichtbar?

## Ablauf
```bash
# 1) Kandidaten finden, laden, transkribieren, prüfen (keine Videos)
python3 clipper.py CHAT.json.gz --video VOD_URL --top 20 --captions --min-len 20 --max-len 40 --review --out OUT
# 2) OUT/review.json lesen, gute Clips auswählen, Titel schreiben -> titles.json
#    {"03_1h02m10s": "LACY tried to rob a bank in GTA 6 💀", ...}
# 3) Nur diese rendern
python3 clipper.py CHAT.json.gz --video VOD_URL --top 20 --captions --min-len 20 --max-len 40 \
    --layout auto --facecam X,Y,W,H --titles titles.json --out OUT
# 4) OUT/*.jpg (Kontaktabzüge) ansehen, erst 1 Vorschau schicken, dann den Rest
```
