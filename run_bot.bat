@echo off
rem ==========================================================
rem GBV 자동매매 봇 실행 스크립트
rem
rem 작업 스케줄러에 등록해 두면 로그온·재부팅 때 자동으로 뜬다.
rem   PowerShell 에서:
rem   Register-ScheduledTask 로 등록 (README 참고)
rem
rem 봇이 단일 실행 잠금을 쥐므로 이미 돌고 있으면 조용히 끝난다.
rem 정상 종료(Ctrl+C)는 종료코드 0 이라 스케줄러가 재시작하지 않는다.
rem ==========================================================
cd /d "%~dp0"

rem 파이썬 경로. 없으면 PATH 의 python 을 쓴다
set PY=C:\Python314\python.exe
if not exist "%PY%" set PY=python

"%PY%" main.py
