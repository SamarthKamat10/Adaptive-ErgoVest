# GitHub upload guide — Adaptive ErgoVest

## What belongs in this repository
Upload the application source code: Python modules, templates, static CSS/JavaScript, tests, requirements files, README, example environment file, batch launcher, and firmware sketch.

## Keep private and off GitHub
Do not upload personal sensor recordings, calibration CSVs, datasets, trained model files, logs, the local SQLite database, `.env`, passwords, API keys, university/private reports, or virtual environments. The `.gitignore` in this folder blocks common versions of these files.

## Before the first commit
1. Check `.env.example`; it should contain placeholders/settings only. Never copy your real `.env` into the repository.
2. Check `README.md` for personal details, private URLs, credentials, or identifiers.
3. Review the staged files using `git status` before committing.
4. This repository includes `trainer.py` and `simulator.py` reconstructed for the WebApp handoff; the original training algorithm was not available for verification. Review these before presenting the ML implementation as final.
5. Physical ESP32 testing has not been verified by this package.

## Windows command-line upload
Create an empty repository on GitHub, then open PowerShell or the VS Code terminal in this folder and run:

```bash
git init
git add .
git status
```

Inspect the status carefully. If private files appear, stop and fix `.gitignore` before proceeding. If correct, run:

```bash
git commit -m "Initial commit: Adaptive ErgoVest source code"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/Adaptive-ErgoVest.git
git push -u origin main
```

Replace `YOUR-USERNAME` with your GitHub username.

## Important
`.gitignore` prevents new untracked files from being added; it does not erase files already committed. If a secret was committed, rotate it immediately and remove it from Git history before making the repository public.
