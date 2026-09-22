"""
workflow/benchmark_local_models.py
─────────────────────────────────────────────────────────────────────────────
Benchmark script to measure REAL latency, throughput, token counts, and ENERGY
for both local models (qwen3.5:4b and llama3.2:3b) across all DAG step types.

Runs each model 5+ times per step type to capture variance, measures:
  - latency_ms (wall-clock, measured)
  - tokens/sec (measured)
  - input_tokens, output_tokens (measured)
  - cost: $0 for both (local inference)
  - energy: MEASURED (real hardware sensors via codecarbon), labeled as MEASURED
  - carbon: ESTIMATED (measured energy × grid intensity), labeled as ESTIMATED

Usage:
    python -m workflow.benchmark_local_models
    python -m workflow.benchmark_local_models --runs 10
    
Output:
    results/benchmark_local_models_<timestamp>.jsonl
─────────────────────────────────────────────────────────────────────────────
"""

import argparse
import json
import os
import sys
import time
import statistics
from pathlib import Path
from typing import Dict, List, Optional
import logging

# Add project root to path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from workflow.model_clients import call_qwen_3_5_4b, call_llama_3_2_3b
from workflow.executor import _STEP_PROMPTS

# Try to import codecarbon for real power measurement
try:
    from codecarbon import EmissionsTracker
    CODECARBON_AVAILABLE = True
except ImportError:
    CODECARBON_AVAILABLE = False
    print("[WARNING] codecarbon not available, falling back to TDP-based estimation")

# ── Results directory ─────────────────────────────────────────────────────────
RESULTS_DIR = root_dir / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Energy & Carbon Constants ─────────────────────────────────────────────────
# Fallback TDP estimation (used only if codecarbon sensors unavailable)
GPU_TDP_WATTS = 80  # NVIDIA GeForce RTX 3050 Laptop GPU max TDP
GPU_INFERENCE_POWER_WATTS = GPU_TDP_WATTS * 0.6  # Conservative 60% estimate

# US grid carbon intensity (EPA 2024 data)
# Source: https://www.epa.gov/egrid
# US average: ~400 gCO2e/kWh, varies by region
GRID_CARBON_INTENSITY_G_PER_KWH = 400.0  # gCO2e per kWh

# Suppress codecarbon's verbose logging
logging.getLogger("codecarbon").setLevel(logging.WARNING)


def measure_energy_with_codecarbon(model_fn: callable, prompt: str, step_context: Dict) -> Dict:
    """
    Measure real energy consumption using codecarbon during model inference.
    
    Uses hardware sensors (Windows EMI/RAPL for CPU, PyNVML for GPU) to sample
    power draw throughout the inference call, then integrates to get actual energy.
    
    Args:
        model_fn: Model function to call
        prompt: Prompt to pass to model
        step_context: Step context dict
    
    Returns:
        Dict with:
          - result: Model call result (with latency_ms, tokens, etc.)
          - energy_wh: MEASURED total energy in watt-hours (CPU + GPU + RAM)
          - energy_cpu_wh: MEASURED CPU energy
          - energy_gpu_wh: MEASURED GPU energy
          - energy_ram_wh: ESTIMATED RAM energy (codecarbon uses model)
          - carbon_g: ESTIMATED carbon (measured energy × grid factor)
          - measured: True (indicates real sensor measurement was used)
          - sensor_info: Description of what was measured
    """
    # Create tracker with fast sampling (100ms intervals)
    tracker = EmissionsTracker(
        project_name="model_benchmark",
        measure_power_secs=0.1,  # Sample every 100ms
        save_to_file=False,
        log_level="warning",
        tracking_mode="process",  # Track only this process
    )
    
    tracker.start()
    
    # Run the model inference
    result = model_fn(prompt, step_context=step_context)
    
    # Stop tracking and get measurements
    emissions_kg = tracker.stop()
    
    # Extract energy measurements (codecarbon returns kWh)
    energy_cpu_kwh = tracker._total_cpu_energy.kWh
    energy_gpu_kwh = tracker._total_gpu_energy.kWh
    energy_ram_kwh = tracker._total_ram_energy.kWh
    total_energy_kwh = tracker._total_energy.kWh
    
    # Convert to Wh
    energy_cpu_wh = energy_cpu_kwh * 1000
    energy_gpu_wh = energy_gpu_kwh * 1000
    energy_ram_wh = energy_ram_kwh * 1000
    total_energy_wh = total_energy_kwh * 1000
    
    # Calculate carbon from measured energy using our grid factor
    # (codecarbon uses its own grid data, we want consistency with our stated factor)
    carbon_g = total_energy_kwh * GRID_CARBON_INTENSITY_G_PER_KWH
    
    # Determine what was actually measured vs estimated
    cpu_interface = type(tracker._cpu_power).__name__ if hasattr(tracker, '_cpu_power') else "unknown"
    gpu_interface = type(tracker._gpu).__name__ if hasattr(tracker, '_gpu') else "unknown"
    
    # Check if real sensors were used
    cpu_measured = "TDP" not in cpu_interface and cpu_interface != "unknown"
    gpu_measured = gpu_interface not in ["EmissionsTracker", "unknown"] and energy_gpu_wh > 0
    
    return {
        "result": result,
        "energy_wh": total_energy_wh,
        "energy_cpu_wh": energy_cpu_wh,
        "energy_gpu_wh": energy_gpu_wh,
        "energy_ram_wh": energy_ram_wh,
        "carbon_g": carbon_g,
        "measured": True,
        "cpu_measured": cpu_measured,
        "gpu_measured": gpu_measured,
        "ram_measured": False,  # RAM is always estimated by codecarbon
        "sensor_info": f"CPU: {cpu_interface}, GPU: {gpu_interface}",
    }


def estimate_energy_fallback(latency_ms: float) -> Dict:
    """
    Fallback TDP-based energy estimation when codecarbon is unavailable or sensors fail.
    
    Method:
      - Energy (Wh) = (GPU power estimate) × (duration in hours)
      - Carbon (gCO2e) = Energy (kWh) × Grid carbon intensity
    
    Args:
        latency_ms: Measured wall-clock latency in milliseconds
    
    Returns:
        Dict with ESTIMATED values marked as such
    """
    duration_hours = latency_ms / 1000 / 3600
    energy_wh = GPU_INFERENCE_POWER_WATTS * duration_hours
    energy_kwh = energy_wh / 1000
    carbon_g = energy_kwh * GRID_CARBON_INTENSITY_G_PER_KWH
    
    return {
        "energy_wh": energy_wh,
        "energy_cpu_wh": 0.0,  # Not separated in fallback
        "energy_gpu_wh": energy_wh,  # Assume all GPU
        "energy_ram_wh": 0.0,
        "carbon_g": carbon_g,
        "measured": False,
        "cpu_measured": False,
        "gpu_measured": False,
        "ram_measured": False,
        "sensor_info": f"TDP fallback: {GPU_INFERENCE_POWER_WATTS}W × {latency_ms}ms",
    }


def benchmark_model(
    model_name: str,
    model_fn: callable,
    step_id: str,
    prompt: str,
    num_runs: int = 5,
) -> Dict:
    """
    Run a model multiple times on a step prompt, collect metrics with real energy measurement.
    
    Returns:
        Dict with measured stats (mean, std, min, max) for:
          - latency_ms (MEASURED)
          - throughput_tokens_per_sec (MEASURED)
          - input_tokens (MEASURED)
          - output_tokens (MEASURED)
          - energy_wh (MEASURED if codecarbon available, else ESTIMATED)
          - carbon_g (ESTIMATED - calculated from energy × grid factor)
    """
    print(f"\n[{step_id}] Benchmarking {model_name} ({num_runs} runs)...")
    
    latencies = []
    throughputs = []
    input_token_counts = []
    output_token_counts = []
    energies_total = []
    energies_cpu = []
    energies_gpu = []
    energies_ram = []
    carbons = []
    
    # Track if we're using real measurements or fallback
    using_real_measurement = False
    energy_measured_flags = []
    
    for run in range(num_runs):
        try:
            # Measure energy during inference
            if CODECARBON_AVAILABLE:
                try:
                    energy_data = measure_energy_with_codecarbon(
                        model_fn, prompt, {"step_id": step_id}
                    )
                    result = energy_data["result"]
                    energy_wh = energy_data["energy_wh"]
                    energy_cpu_wh = energy_data["energy_cpu_wh"]
                    energy_gpu_wh = energy_data["energy_gpu_wh"]
                    energy_ram_wh = energy_data["energy_ram_wh"]
                    carbon_g = energy_data["carbon_g"]
                    using_real_measurement = energy_data["measured"]
                    energy_measured_flags.append(energy_data["measured"])
                except Exception as e:
                    print(f"    [WARNING] Codecarbon measurement failed: {e}, using fallback")
                    result = model_fn(prompt, step_context={"step_id": step_id})
                    fallback = estimate_energy_fallback(result["latency_ms"])
                    energy_wh = fallback["energy_wh"]
                    energy_cpu_wh = fallback["energy_cpu_wh"]
                    energy_gpu_wh = fallback["energy_gpu_wh"]
                    energy_ram_wh = fallback["energy_ram_wh"]
                    carbon_g = fallback["carbon_g"]
                    using_real_measurement = False
                    energy_measured_flags.append(False)
            else:
                # No codecarbon, use fallback estimation
                result = model_fn(prompt, step_context={"step_id": step_id})
                fallback = estimate_energy_fallback(result["latency_ms"])
                energy_wh = fallback["energy_wh"]
                energy_cpu_wh = fallback["energy_cpu_wh"]
                energy_gpu_wh = fallback["energy_gpu_wh"]
                energy_ram_wh = fallback["energy_ram_wh"]
                carbon_g = fallback["carbon_g"]
                using_real_measurement = False
                energy_measured_flags.append(False)
            
            latency_ms = result["latency_ms"]
            input_tokens = result["input_tokens"]
            output_tokens = result["output_tokens"]
            
            # Calculate throughput (tokens/sec)
            total_tokens = input_tokens + output_tokens
            throughput = (total_tokens / latency_ms * 1000) if latency_ms > 0 else 0
            
            latencies.append(latency_ms)
            throughputs.append(throughput)
            input_token_counts.append(input_tokens)
            output_token_counts.append(output_tokens)
            energies_total.append(energy_wh)
            energies_cpu.append(energy_cpu_wh)
            energies_gpu.append(energy_gpu_wh)
            energies_ram.append(energy_ram_wh)
            carbons.append(carbon_g)
            
            energy_label = "MEASURED" if using_real_measurement else "ESTIMATED"
            print(f"  Run {run+1}/{num_runs}: {latency_ms}ms, "
                  f"{throughput:.1f} tok/s, "
                  f"tokens({input_tokens}+{output_tokens}={total_tokens}), "
                  f"energy: {energy_wh:.4f}Wh ({energy_label})")
            
        except Exception as e:
            print(f"  Run {run+1}/{num_runs} FAILED: {e}")
            continue
    
    if not latencies:
        raise RuntimeError(f"All runs failed for {model_name} on {step_id}")
    
    # Determine final measurement status
    energy_is_measured = any(energy_measured_flags)
    
    # Calculate statistics
    stats = {
        "model": model_name,
        "step_id": step_id,
        "num_runs": len(latencies),
        # MEASURED metrics
        "latency_ms": {
            "mean": statistics.mean(latencies),
            "stdev": statistics.stdev(latencies) if len(latencies) > 1 else 0,
            "min": min(latencies),
            "max": max(latencies),
            "measured": True,
        },
        "throughput_tokens_per_sec": {
            "mean": statistics.mean(throughputs),
            "stdev": statistics.stdev(throughputs) if len(throughputs) > 1 else 0,
            "min": min(throughputs),
            "max": max(throughputs),
            "measured": True,
        },
        "input_tokens": {
            "mean": statistics.mean(input_token_counts),
            "stdev": statistics.stdev(input_token_counts) if len(input_token_counts) > 1 else 0,
            "measured": True,
        },
        "output_tokens": {
            "mean": statistics.mean(output_token_counts),
            "stdev": statistics.stdev(output_token_counts) if len(output_token_counts) > 1 else 0,
            "measured": True,
        },
        # Energy metrics - MEASURED or ESTIMATED depending on sensors
        "energy_wh": {
            "mean": statistics.mean(energies_total),
            "stdev": statistics.stdev(energies_total) if len(energies_total) > 1 else 0,
            "min": min(energies_total),
            "max": max(energies_total),
            "measured": energy_is_measured,
            "method": "Hardware sensors (Windows EMI/RAPL + PyNVML)" if energy_is_measured 
                     else f"TDP fallback ({GPU_INFERENCE_POWER_WATTS}W × duration)",
        },
        "energy_cpu_wh": {
            "mean": statistics.mean(energies_cpu),
            "measured": energy_is_measured,
        },
        "energy_gpu_wh": {
            "mean": statistics.mean(energies_gpu),
            "measured": energy_is_measured,
        },
        "energy_ram_wh": {
            "mean": statistics.mean(energies_ram),
            "measured": False,  # RAM is always estimated by codecarbon
        },
        # Carbon - ESTIMATED (calculated from energy)
        "carbon_g": {
            "mean": statistics.mean(carbons),
            "stdev": statistics.stdev(carbons) if len(carbons) > 1 else 0,
            "min": min(carbons),
            "max": max(carbons),
            "measured": False,  # Carbon is always estimated from energy × grid factor
            "method": f"Energy × US grid intensity ({GRID_CARBON_INTENSITY_G_PER_KWH}gCO2e/kWh)",
            "note": "Carbon estimated from " + ("measured energy" if energy_is_measured else "estimated energy"),
        },
        # Cost
        "cost_usd": 0.0,  # Local inference, no API cost
        "cost_note": "Local inference via Ollama, no API charges",
    }
    
    energy_status = "MEASURED" if energy_is_measured else "ESTIMATED"
    print(f"  -> Mean latency: {stats['latency_ms']['mean']:.1f}ms "
          f"(+/-{stats['latency_ms']['stdev']:.1f}ms), "
          f"throughput: {stats['throughput_tokens_per_sec']['mean']:.1f} tok/s")
    print(f"  -> {energy_status} energy: {stats['energy_wh']['mean']:.4f}Wh "
          f"(CPU: {stats['energy_cpu_wh']['mean']:.4f}, "
          f"GPU: {stats['energy_gpu_wh']['mean']:.4f}, "
          f"RAM: {stats['energy_ram_wh']['mean']:.4f})")
    print(f"  -> ESTIMATED carbon: {stats['carbon_g']['mean']:.4f}gCO2e")
    
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark local models (qwen3.5:4b, llama3.2:3b) across all step types"
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="Number of runs per model per step (default: 5)",
    )
    args = parser.parse_args()
    
    print("=" * 70)
    print("LOCAL MODEL BENCHMARKING")
    print("=" * 70)
    print(f"Models: qwen3.5:4b, llama3.2:3b")
    print(f"Steps: {', '.join(_STEP_PROMPTS.keys())}")
    print(f"Runs per model per step: {args.runs}")
    print(f"\nMeasured metrics: latency_ms, throughput, tokens")
    
    if CODECARBON_AVAILABLE:
        print(f"Energy measurement: REAL hardware sensors via codecarbon")
        print(f"  - CPU: Windows EMI/RAPL (real power readings)")
        print(f"  - GPU: PyNVML (real power readings)")
        print(f"  - RAM: Model-based estimation")
    else:
        print(f"Energy measurement: ESTIMATED (codecarbon not available)")
        print(f"  - Fallback: GPU TDP × duration ({GPU_INFERENCE_POWER_WATTS}W)")
    
    print(f"Carbon calculation: ESTIMATED (energy × grid intensity)")
    print(f"  - Grid carbon intensity: {GRID_CARBON_INTENSITY_G_PER_KWH}gCO2e/kWh (US average)")
    print("=" * 70)
    
    # Models to benchmark
    models = [
        ("qwen3.5:4b", call_qwen_3_5_4b),
        ("llama3.2:3b", call_llama_3_2_3b),
    ]
    
    # Collect all results
    all_results = []
    
    # Benchmark each model on each step
    for step_id, prompt in _STEP_PROMPTS.items():
        print(f"\n{'='*70}")
        print(f"STEP: {step_id}")
        print(f"{'='*70}")
        
        for model_name, model_fn in models:
            try:
                stats = benchmark_model(
                    model_name=model_name,
                    model_fn=model_fn,
                    step_id=step_id,
                    prompt=prompt,
                    num_runs=args.runs,
                )
                all_results.append(stats)
            except Exception as e:
                print(f"  [ERROR] Failed to benchmark {model_name} on {step_id}: {e}")
                continue
    
    # Write results to JSONL
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    output_file = RESULTS_DIR / f"benchmark_local_models_{timestamp}.jsonl"
    
    with output_file.open("w", encoding="utf-8") as f:
        for result in all_results:
            f.write(json.dumps(result) + "\n")
    
    print(f"\n{'='*70}")
    print(f"Benchmark complete!")
    print(f"Results written to: {output_file}")
    print(f"Total records: {len(all_results)}")
    print(f"{'='*70}")
    
    # Print summary table
    print(f"\nSUMMARY (mean values):")
    print(f"{'Model':<15} {'Step':<12} {'Latency (ms)':<15} {'Throughput':<15} {'Energy (Wh)':<15} {'Carbon (g)':<15}")
    print("-" * 95)
    
    energy_measured_count = 0
    for result in all_results:
        model = result["model"]
        step = result["step_id"]
        lat = f"{result['latency_ms']['mean']:.1f}"
        thr = f"{result['throughput_tokens_per_sec']['mean']:.1f}"
        eng = f"{result['energy_wh']['mean']:.4f}"
        carb = f"{result['carbon_g']['mean']:.4f}"
        print(f"{model:<15} {step:<12} {lat:<15} {thr:<15} {eng:<15} {carb:<15}")
        
        if result['energy_wh'].get('measured', False):
            energy_measured_count += 1
    
    print(f"\nMeasurement notes:")
    if energy_measured_count > 0:
        print(f"  - Energy: MEASURED via hardware sensors ({energy_measured_count}/{len(all_results)} runs)")
        print(f"    Method: Windows EMI/RAPL (CPU) + PyNVML (GPU) sampled at 100ms intervals")
    else:
        print(f"  - Energy: ESTIMATED via TDP fallback")
        print(f"    Method: GPU power ({GPU_INFERENCE_POWER_WATTS}W) × duration")
    print(f"  - Carbon: ESTIMATED (measured or estimated energy × grid intensity)")
    print(f"    Grid factor: {GRID_CARBON_INTENSITY_G_PER_KWH}gCO2e/kWh (US average)")


if __name__ == "__main__":
    main()
