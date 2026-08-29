.PHONY: dev build test install uninstall open

dev:
	python3 -m terminal_agent_launcher serve

build:
	python3 -m compileall -q terminal_agent_launcher agent_launchpad
	node --check terminal_agent_launcher/web/assets/lib.js
	node --check terminal_agent_launcher/web/assets/app.js

test:
	python3 -m unittest discover -v
	node --test tests/web.test.mjs

install:
	python3 -m terminal_agent_launcher install

uninstall:
	python3 -m terminal_agent_launcher uninstall

open:
	python3 -m terminal_agent_launcher open
