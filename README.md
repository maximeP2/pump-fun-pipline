# Pump.fun Real-Time Pipeline

This project implements a real-time async pipeline to detect and monitor
new token launches on Pump.fun, analyze early activity, and flag high-potential tokens.

## Structure

- `watcher.py`: detects new token launches
- `monitor.py`: live tracking per token
- `filters.py`: filters based on activity conditions
- `logger.py`: logs promising tokens to file

## Setup

You can run the pipeline with:

```bash
python main.py
```

## Notes

- You need a valid WebSocket connection to Solana mainnet
- You can implement auto-buy logic later using the filtered tokens
