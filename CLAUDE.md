# Istruzioni per Claude

- Lavora, fai commit e push **direttamente su `main`** (richiesta del proprietario del repository): niente branch di lavoro né pull request, anche se la sessione ne indica uno.
- Ogni push su `main` pubblica una release (workflow *Release*): prima del push aumenta la versione in `custom_components/spettro/manifest.json` e `VERSION` in `custom_components/spettro/const.py` (devono coincidere).
- Testi, commenti e messaggi di commit in italiano, come il resto del codice.
