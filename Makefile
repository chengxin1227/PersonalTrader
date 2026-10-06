.PHONY: setup monitor once test-slack test-pushover test agent agent-stop

setup:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
	@if [ ! -f .env ]; then cp .env.example .env; fi
	@if [ ! -f config/rules.yaml ]; then cp config/rules.example.yaml config/rules.yaml; fi
	@echo "Fill in API keys in .env. Slack or Pushover is required for alerts."

monitor:
	.venv/bin/python -m monitor

once:
	.venv/bin/python -m monitor --once -v

once-moomoo:
	.venv/bin/python -m monitor --source moomoo --once -v

test-slack:
	.venv/bin/python -m monitor --test-slack

test-pushover:
	.venv/bin/python -m monitor --test-pushover

test:
	.venv/bin/python -m pytest tests

agent:
	mkdir -p "$(HOME)/Library/Logs/PersonalTrader" "$(HOME)/Library/LaunchAgents"
	cp scripts/com.personaltrader.monitor.plist "$(HOME)/Library/LaunchAgents/com.personaltrader.monitor.plist"
	-launchctl bootout gui/$$(id -u) "$(HOME)/Library/LaunchAgents/com.personaltrader.monitor.plist"
	launchctl bootstrap gui/$$(id -u) "$(HOME)/Library/LaunchAgents/com.personaltrader.monitor.plist"
	launchctl enable gui/$$(id -u)/com.personaltrader.monitor
	launchctl kickstart -k gui/$$(id -u)/com.personaltrader.monitor

agent-stop:
	launchctl bootout gui/$$(id -u) "$(HOME)/Library/LaunchAgents/com.personaltrader.monitor.plist"
