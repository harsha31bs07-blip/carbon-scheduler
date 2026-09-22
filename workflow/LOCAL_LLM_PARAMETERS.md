# Local LLM Parameter Analysis for Dashboard
**Model:** llama3.2:3b via Ollama  
**Generated:** 2026-09-22  
**Purpose:** Cost, energy, and carbon parameters for scheduler integration and dashboard display

---

## Model Specifications

### llama3.2:3b (local-llama3.2)
- **Provider:** Ollama (local inference)
- **Model size:** 3 billion parameters
- **Quantization:** Assumed 4-bit (typical for Ollama 3B models)
- **Deployment:** Local workstation/laptop inference

---

## Cost Analysis

### API Cost
**$0.00** per request (no API charges)

**Labeling requirement:** All dashboard displays must show:
- "Local inference - no API cost"
- Cost field: `$0.00` with asterisk: "* Local model, no cloud API charges"

**Comparison to cloud models** (from data/model_table.py):
- Haiku (gemini-3-flash): $0.001-$0.005 per 1k tokens (input/output)
- Sonnet (nemotron-ultra-3): $0.002-$0.010 per 1k tokens
- Opus (gemini-3-pro): $0.005-$0.025 per 1k tokens

**Savings:** 100% cost reduction vs. any cloud model for equivalent workloads

---

## Energy Analysis

### ⚠️ ESTIMATED - Not Measured

**Method:** Laptop TDP-based estimation during inference
**Assumption:** Typical consumer laptop GPU/CPU power draw

### Baseline Assumptions:
1. **Hardware profile:** Mid-range consumer laptop (AMD Ryzen/Intel Core i5-i7 class)
   - CPU TDP: 15-28W (typical laptop U-series or H-series)
   - iGPU boost: +10-20W during inference (if using integrated graphics)
   - **Total system draw during inference:** ~35W average

2. **Duration:** Based on measured benchmark latency
   - Classify: 5.4s average
   - Retrieve: 3.1s average
   - Draft: 3.4s average
   - Summarize: 2.5s average
   - **Average per request:** 3.6s

3. **Energy per request (ESTIMATED):**
   ```
   Energy = Power × Time
   Energy = 35W × 3.6s = 126 Wh/s = 0.000035 kWh per request
   ```

### Comparison to Cloud Models (from data/model_table.py):
Cloud model energy includes datacenter PUE (Power Usage Effectiveness) overhead.

**Per 1k tokens (blended 75% input / 25% output):**
- Haiku: 0.000028 kWh/1k tokens
- Sonnet: 0.000168 kWh/1k tokens  
- Opus: 0.000896 kWh/1k tokens

**Local LLM (llama3.2:3b) per request:**
- **0.000035 kWh per request (ESTIMATED)**
- Assuming ~50 tokens average per request → **0.00007 kWh/1k tokens (ESTIMATED)**

**Analysis:**
- Local model ESTIMATED energy: **2.5x Haiku's energy** per 1k tokens
- However, this is **apples-to-oranges**: Local includes full system overhead (display, OS, etc.), while cloud models show only inference energy at the datacenter
- **Tradeoff:** Higher local energy per token BUT eliminates network transmission energy and datacenter cooling overhead

### Dashboard Labeling Requirements:
**CRITICAL:** All energy values for local models MUST be labeled:
- "ESTIMATED (local system TDP × duration)"
- "Based on 35W laptop TDP assumption"
- "Not directly comparable to measured cloud model values"

---

## Carbon Analysis

### ⚠️ ESTIMATED - Not Measured

**Method:** Grid carbon intensity × estimated energy consumption

### Assumptions:
1. **Grid carbon intensity:** Depends on user location and time
   - US average: ~400 gCO2/kWh (2026 grid mix)
   - EU average: ~250 gCO2/kWh (higher renewable penetration)
   - **Assumption for analysis:** 400 gCO2/kWh (US grid mix)

2. **Carbon per request (ESTIMATED):**
   ```
   Carbon = Energy × Grid Intensity
   Carbon = 0.000035 kWh × 400 gCO2/kWh = 0.014 gCO2 per request
   ```

### Comparison to Cloud Models:
Cloud models use data/region_carbon.py which provides time-varying carbon intensity by region.

**Typical cloud model carbon (from scheduler):**
- Haiku @ us-east, now: ~0.005-0.015 gCO2 per request (varies by time/region)
- Sonnet @ us-east, now: ~0.030-0.080 gCO2 per request
- Opus @ us-east, now: ~0.150-0.400 gCO2 per request

**Local LLM (llama3.2:3b):**
- **0.014 gCO2 per request (ESTIMATED, US grid assumption)**
- Equivalent to Haiku in high-carbon windows
- ~2-10x better than Sonnet/Opus

**Key insight:** Local model carbon is LOCATION-DEPENDENT:
- Clean grid (EU/renewable-heavy): ~0.009 gCO2 per request
- Dirty grid (coal-heavy): ~0.025+ gCO2 per request
- Cloud models can shift to low-carbon regions; local cannot

### Dashboard Labeling Requirements:
**CRITICAL:** All carbon values for local models MUST be labeled:
- "ESTIMATED (based on local grid assumption)"
- "Assumes 400 gCO2/kWh US average grid mix"
- "Actual carbon varies by location and time of day"
- "Not directly comparable to cloud models with region selection"

---

## Latency Analysis

### ✓ MEASURED - From Benchmark

**Real wall-clock latency** (measured in workflow/local_llm_benchmark_fast.py):

| Step       | Measured Latency | SLA Target | Status   |
|------------|------------------|------------|----------|
| classify   | 5,436 ms        | 2,000 ms   | MISS     |
| retrieve   | 3,131 ms        | 3,000 ms   | MISS     |
| draft      | 3,351 ms        | 5,000 ms   | **OK**   |
| summarize  | 2,549 ms        | 2,000 ms   | MISS     |
| **Average**| **3,617 ms**    | 3,000 ms   | MISS     |

**Throughput:** 68 tokens/sec (consistent across all steps)

**Comparison to cloud models:**
- Cloud models (Gemini/Nemotron): ~400-1000ms typical latency
- Local model: **3-10x slower** than cloud alternatives
- **Tradeoff:** Higher latency BUT zero API cost and full data privacy

---

## Accuracy Analysis

### Not Measured

**Note:** Benchmark focused on performance, not accuracy. Accuracy evaluation would require:
1. Running data/eval/run_eval.py against local-llama3.2
2. Comparing outputs to reference answers in data/eval/eval_set.json
3. Computing match scores per task category

**Assumed baseline for scheduler:** 0.70 (between Haiku 0.76 and below Sonnet 0.88)

**Recommendation:** Use assumed value until formal eval is run. Local 3B models typically score lower than cloud models on complex reasoning but are adequate for short-form classification/summarization tasks.

---

## Summary Table for Dashboard

| Metric               | local-llama3.2      | Haiku (cloud)    | Sonnet (cloud)   | Opus (cloud)     |
|----------------------|---------------------|------------------|------------------|------------------|
| **Cost per request** | $0.00 (local)       | ~$0.0015         | ~$0.003          | ~$0.0075         |
| **Energy per req**   | 0.000035 kWh (EST)  | ~0.000008 kWh    | ~0.000050 kWh    | ~0.000250 kWh    |
| **Carbon per req**   | 0.014 gCO2 (EST)    | 0.003-0.015 gCO2 | 0.030-0.080 gCO2 | 0.150-0.400 gCO2 |
| **Latency**          | 3,617 ms (measured) | ~600 ms          | ~1,200 ms        | ~2,500 ms        |
| **Throughput**       | 68 tok/s (measured) | 150 tok/s        | 80 tok/s         | 45 tok/s         |
| **Accuracy**         | 0.70 (assumed)      | 0.76             | 0.88             | 0.94             |

---

## Scheduler Integration Requirements

### For Person B (Scheduler) to recognize local model as a valid candidate:

**Option 1: Extend existing model catalog**
Add to `data/model_table.py`:
```python
"local-llama3.2": ModelSpec(
    canonical_name="local-llama3.2",
    display_name="Llama 3.2 (Local)",
    api_model_id="local-llama3.2",
    cost_input_per_1k_usd=0.0,  # No API cost
    cost_output_per_1k_usd=0.0,
    energy_in_per_1k_kwh=0.00007 * 0.75,  # ESTIMATED, split 75/25
    energy_out_per_1k_kwh=0.00007 * 0.25,
    ttft_ms=1000.0,  # Estimated from benchmark
    output_tokens_per_sec=68.0,  # Measured
    base_accuracy=0.70,  # Assumed
)
```

**Option 2: Add as new region "local" with special handling**
Treat local execution as a pseudo-region with:
- region="local"
- carbon_intensity=400 gCO2/kWh (configurable)
- latency=0 (no network overhead)

**Recommendation:** Option 1 is cleaner — add to MODEL_TABLE so scheduler sees it as a standard candidate.

### What Person B needs to add:
1. Recognize "local-llama3.2" in candidate_gen.py
2. Handle cost=0 edge case in scorer.py (avoid divide-by-zero if normalizing)
3. Label energy/carbon as ESTIMATED in any logs/outputs

**This is a data/ schema change — outside workflow/ scope. Report precisely what's needed rather than implementing.**

---

## Policy Behavior Predictions

### How local model should be selected under each policy:

1. **Eco policy** (minimize carbon):
   - **Selected IF:** Local grid is cleaner than cloud options (~<300 gCO2/kWh)
   - **NOT selected IF:** High-carbon local grid (>450 gCO2/kWh) vs. low-carbon cloud region

2. **Speed policy** (minimize latency):
   - **NEVER selected** — 3.6s avg vs <1s for cloud models

3. **Balanced policy** (quality per task):
   - **MAY be selected for simple tasks** (classify, summarize) if cost weight is high
   - **NOT selected for complex tasks** (retrieve, draft) due to lower accuracy

4. **Custom policy** (user-defined weights):
   - **Selected when cost weight >0.5** — only option with $0 cost
   - **Selected when w_cost=1.0, others=0.0** — pure cost minimization

**Expected outcome:** Local model should finally differentiate policies, breaking the "all identical" problem if cost weight varies.

---

## Next Steps (STEP 4)

1. **Report to Person B:** Local model needs entry in data/model_table.py (see Option 1 above)
2. **Test scheduler recognition:** Can scheduler generate local-llama3.2 as a candidate?
3. **Run policy sweep:** Execute full pipeline under eco/speed/balanced/custom with local option available
4. **Verify differentiation:** Confirm policies now produce different selections (local vs cloud)

---

**Generated:** 2026-09-22  
**Measurement status:** Latency MEASURED, energy/carbon ESTIMATED  
**Verification needed:** Scheduler integration (Person B's data/ module)
