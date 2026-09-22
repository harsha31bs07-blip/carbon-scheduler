"""
Complete benchmark: 2 models × 4 steps × 5 runs each = 40 measurements
With corrected thinking-mode settings per step.
"""
import sys
sys.path.insert(0, '.')

from workflow.model_clients import call_qwen_3_5_4b, call_llama_3_2_3b
from workflow.executor import _STEP_PROMPTS
import time
import json

# Per-step thinking mode settings for qwen3.5
THINKING_MODE = {
    "classify": False,   # Fast, mechanical
    "retrieve": False,   # Knowledge lookup
    "draft": True,       # Complex generation - test with thinking ON
    "summarize": False,  # Concise output
}

GPU_POWER_WATTS = 48.0
GRID_CARBON_G_PER_KWH = 400.0

results = []

print("="*80)
print("FULL LOCAL MODEL BENCHMARK")
print("="*80)
print("Models: qwen3.5:4b (thinking OFF except draft), llama3.2:3b")
print("Steps: classify, retrieve, draft, summarize")
print("Runs per combination: 5")
print("="*80)

for step_id, prompt in _STEP_PROMPTS.items():
    print(f"\n{'='*80}")
    print(f"STEP: {step_id}")
    print(f"{'='*80}")
    
    # qwen3.5:4b with per-step thinking mode
    print(f"\nqwen3.5:4b (thinking={'ON' if THINKING_MODE[step_id] else 'OFF'}):")
    qwen_latencies = []
    qwen_tokens = []
    
    for run in range(5):
        result = call_qwen_3_5_4b(prompt, enable_thinking=THINKING_MODE[step_id])
        lat = result["latency_ms"]
        tok = result["input_tokens"] + result["output_tokens"]
        qwen_latencies.append(lat)
        qwen_tokens.append(tok)
        
        duration_h = lat / 1000 / 3600
        energy_wh = GPU_POWER_WATTS * duration_h
        carbon_g = (energy_wh / 1000) * GRID_CARBON_G_PER_KWH
        
        print(f"  Run {run+1}: {lat:.0f}ms, {result['input_tokens']}+{result['output_tokens']}={tok} tokens")
        
        if run == 4:  # Save last run
            results.append({
                "model": "qwen3.5:4b",
                "step_id": step_id,
                "thinking_mode": THINKING_MODE[step_id],
                "latency_ms_MEASURED": lat,
                "latency_ms_avg": sum(qwen_latencies) / len(qwen_latencies),
                "latency_ms_min": min(qwen_latencies),
                "latency_ms_max": max(qwen_latencies),
                "input_tokens_MEASURED": result["input_tokens"],
                "output_tokens_MEASURED": result["output_tokens"],
                "total_tokens_avg": sum(qwen_tokens) / len(qwen_tokens),
                "throughput_tokens_per_sec_MEASURED": tok / (lat / 1000),
                "cost_usd": 0.0,
                "energy_wh_ESTIMATED": energy_wh,
                "carbon_g_ESTIMATED": carbon_g,
            })
    
    avg_qwen = sum(qwen_latencies) / len(qwen_latencies)
    print(f"  -> Average: {avg_qwen:.0f}ms, {sum(qwen_tokens)/5:.0f} tokens")
    
    # llama3.2:3b (no thinking mode)
    print(f"\nllama3.2:3b:")
    llama_latencies = []
    llama_tokens = []
    
    for run in range(5):
        result = call_llama_3_2_3b(prompt)
        lat = result["latency_ms"]
        tok = result["input_tokens"] + result["output_tokens"]
        llama_latencies.append(lat)
        llama_tokens.append(tok)
        
        duration_h = lat / 1000 / 3600
        energy_wh = GPU_POWER_WATTS * duration_h
        carbon_g = (energy_wh / 1000) * GRID_CARBON_G_PER_KWH
        
        print(f"  Run {run+1}: {lat:.0f}ms, {result['input_tokens']}+{result['output_tokens']}={tok} tokens")
        
        if run == 4:  # Save last run
            results.append({
                "model": "llama3.2:3b",
                "step_id": step_id,
                "thinking_mode": None,
                "latency_ms_MEASURED": lat,
                "latency_ms_avg": sum(llama_latencies) / len(llama_latencies),
                "latency_ms_min": min(llama_latencies),
                "latency_ms_max": max(llama_latencies),
                "input_tokens_MEASURED": result["input_tokens"],
                "output_tokens_MEASURED": result["output_tokens"],
                "total_tokens_avg": sum(llama_tokens) / len(llama_tokens),
                "throughput_tokens_per_sec_MEASURED": tok / (lat / 1000),
                "cost_usd": 0.0,
                "energy_wh_ESTIMATED": energy_wh,
                "carbon_g_ESTIMATED": carbon_g,
            })
    
    avg_llama = sum(llama_latencies) / len(llama_latencies)
    print(f"  -> Average: {avg_llama:.0f}ms, {sum(llama_tokens)/5:.0f} tokens")
    
    # Comparison
    if avg_qwen < avg_llama:
        print(f"  *** qwen3.5 is {((avg_llama-avg_qwen)/avg_llama*100):.1f}% faster")
    else:
        print(f"  *** llama3.2 is {((avg_qwen-avg_llama)/avg_qwen*100):.1f}% faster")

# Write results
timestamp = time.strftime("%Y%m%d_%H%M%S")
output_file = f"results/benchmark_local_FINAL_{timestamp}.jsonl"
with open(output_file, 'w') as f:
    for r in results:
        f.write(json.dumps(r) + '\n')

print(f"\n{'='*80}")
print(f"BENCHMARK COMPLETE")
print(f"Results: {output_file}")
print(f"Total measurements: {len(results)}")
print(f"{'='*80}")

# Summary table
print(f"\nSUMMARY TABLE (average latency):")
print(f"{'Model':<15} {'Step':<12} {'Thinking':<10} {'Latency':<12} {'Tokens':<10} {'Energy*':<12} {'Carbon*'}")
print("-"*85)
for r in results:
    thinking = "ON" if r["thinking_mode"] else "OFF" if r["thinking_mode"] is not None else "N/A"
    print(f"{r['model']:<15} {r['step_id']:<12} {thinking:<10} {r['latency_ms_avg']:.0f}ms{'':<7} {r['total_tokens_avg']:.0f}{'':<5} {r['energy_wh_ESTIMATED']:.4f}Wh{'':<3} {r['carbon_g_ESTIMATED']:.4f}g")

print(f"\n* Energy and carbon are ESTIMATED (GPU 48W × duration, US grid 400g/kWh)")
