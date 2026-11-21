"""Sight Engine Package for CLIP AUTO.

Extracts visual understanding and scene state at 2 fps:
- Faces and 5-point landmarks (OpenCV YuNet)
- Person counting (License-clean RT-DETR / YOLOX)
- Known person face recognition (OpenCV SFace)
- Active mouth movement per face
- Facecam panel tracking & layout discovery
- Shot/Scene transitions (TransNetV2)
- On-screen text & game subtitles (RapidOCR)
- Visual description (Qwen2.5-VL / Cloud Cascade)
"""
