<!-- DRAFT. Not posted. Every number is from results/ in this repository (see REPORT.md). -->

# Series 2: Running LLM-written code safely, on a budget you cannot exceed

## Post 1/3: The sandbox has exactly one network peer

The harness code in lamarck is written by an LLM, so it is untrusted. Each evaluation runs in a fresh
gVisor container: read-only root and mounts, no capabilities, CPU / memory / pid limits, and an
internal-only network whose only peer is an LLM gateway that holds the API key.

Escape probes run in CI: external IPs, DNS, cloud metadata, the host, the Docker socket, raw sockets,
writes to read-only mounts. All fail; only the gateway answers.

One surprise: gVisor cannot reach Docker's embedded DNS, so the gateway's address is injected with
--add-host. Side effect: there is no DNS in the sandbox at all.

## Post 2/3: The day the credit ran out

Mid-experiment, the provider account ran out of credit and started answering HTTP 403. My evaluator
scored each 403 as a wrong answer. Several runs reported 0/120 held-out. 2,138 calls got a 403 before I
noticed.

The fix is conceptual: a provider failure measures the infrastructure, not the agent. Such an
evaluation is now discarded, never cached, and the run pauses so it can resume later. The incident is
written down in a validity file next to the results, with the first 403's timestamp and which runs were
set aside.

## Post 3/3: A money cap that cannot be crossed

Then I had about RMB 12 left, so the gateway learned to count money:
- before each call it reserves a worst-case price (every request character as one token, plus
  max_tokens of output);
- it refuses the call if spent + in flight + worst case would cross the cap;
- after the call it settles at the price of the usage the API returned, cached tokens at the cached rate.

The scheduler only starts an experiment if the money left covers it and every higher-priority
experiment. The whole revised study cost RMB 5.384 for 5,184 calls; 58% of the policy's prompt tokens
were served from the provider's cache.
