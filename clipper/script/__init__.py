"""Script Engine Package for CLIP AUTO.

Fuses hearing and sight representations into an aligned timeline script:
- Voice-to-person linking (mouth movement + audio alignment)
- Game subtitle verification (RapidOCR vs Whisper)
- Live person vs Media voice reconciliation
- Emits hearing.jsonl, sight.jsonl, and merged script.jsonl / script.txt
"""
