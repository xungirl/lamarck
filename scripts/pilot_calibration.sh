#!/usr/bin/env bash
# Seed-harness baselines: 3 evaluations (rollouts 0,1,2) on each evolve split (these double as the
# noise-band calibration of runs that start from the seed harness) + one held-out evaluation.
set -euo pipefail
L="uv run python -m lamarck.cli"
$L eval --domain text2sql --split spider_evolve --rollouts 0 1 2
$L eval --domain text2sql --split spider_heldout --rollouts 0
$L eval --domain text2sql --split bird_evolve --rollouts 0 1 2
$L eval --domain text2sql --split bird_heldout --rollouts 0
$L eval --domain text2sql --split bird_evolve --model deepseek-v4.1-flash --rollouts 0 1 2
$L eval --domain text2sql --split bird_heldout --model deepseek-v4.1-flash --rollouts 0
$L eval --domain supportops --split so_evolve --rollouts 0 1 2
$L eval --domain supportops --split so_heldout --rollouts 0
$L eval --domain supportops --split ood_evolve --rollouts 0 1 2
$L eval --domain supportops --split ood_heldout --rollouts 0
