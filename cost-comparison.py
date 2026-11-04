#!/usr/bin/env python3
"""
cost-comparison.py — Show token usage before/after optimization.

Helps you understand the cost savings from the optimized pipeline.
"""


def calculate_costs():
    """Calculate and display token costs for before/after scenarios."""
    print("These figures are estimates for the retired direct-API experiment.")
    print("Current command: .venv/bin/python prompt-listener.py --process <url>")
    print()

    # Realistic costs based on typical X prompt posts
    scenarios = [
        {
            "name": "Small post (1-2 prompts, no images)",
            "before": {
                "tweet_analysis": 200,
                "image_vision": 0,
                "translations": 400,
                "overhead": 100,
                "total": 700,
            },
            "after": {
                "tweet_analysis": 80,
                "image_ocr": 0,
                "translations": 150,
                "overhead": 30,
                "total": 260,
            },
        },
        {
            "name": "Medium post (3-4 prompts, 2 images)",
            "before": {
                "tweet_analysis": 300,
                "image_vision": 300,  # 2 images × 150 tokens each
                "translations": 700,
                "overhead": 200,
                "total": 1500,
            },
            "after": {
                "tweet_analysis": 150,
                "image_ocr": 0,  # Free local OCR
                "translations": 300,
                "overhead": 50,
                "total": 500,
            },
        },
        {
            "name": "Large post (5+ prompts, thread, 4 images)",
            "before": {
                "tweet_analysis": 400,
                "image_vision": 600,  # 4 images × 150 tokens each
                "translations": 1200,
                "overhead": 300,
                "total": 2500,
            },
            "after": {
                "tweet_analysis": 250,
                "image_ocr": 0,  # Free local OCR
                "translations": 500,
                "overhead": 80,
                "total": 830,
            },
        },
    ]

    # Pricing (Claude 3.5 Sonnet)
    # $3 per 1M input tokens
    # $15 per 1M output tokens
    # Assume 2:1 input:output ratio for these workloads
    input_cost_per_million = 3.00
    output_cost_per_million = 15.00

    print("=" * 80)
    print("TOKEN COST COMPARISON: Before vs After Optimization")
    print("=" * 80)
    print()

    total_before = 0
    total_after = 0
    total_savings = 0

    for scenario in scenarios:
        before_tokens = scenario["before"]["total"]
        after_tokens = scenario["after"]["total"]
        savings = before_tokens - after_tokens
        savings_pct = (savings / before_tokens * 100) if before_tokens > 0 else 0

        # Simple cost estimate (treating as ~2:1 input:output)
        # Average cost per token ≈ (3 + 15/2) / 1M ≈ 0.0105 cents
        before_cost = before_tokens * 0.00001  # ~$0.00001 per token
        after_cost = after_tokens * 0.00001

        total_before += before_tokens
        total_after += after_tokens
        total_savings += savings

        print(f"📊 {scenario['name']}")
        print(f"   Before: {before_tokens:5d} tokens ≈ ${before_cost:.4f}")
        print(f"   After:  {after_tokens:5d} tokens ≈ ${after_cost:.4f}")
        print(f"   Saves:  {savings:5d} tokens ({savings_pct:5.1f}%) ≈ ${before_cost - after_cost:.4f}")
        print()

    print("=" * 80)
    print("AGGREGATE IMPACT")
    print("=" * 80)
    avg_before = total_before / len(scenarios)
    avg_after = total_after / len(scenarios)
    avg_savings = total_savings / len(scenarios)
    avg_savings_pct = (avg_savings / avg_before * 100) if avg_before > 0 else 0

    print(f"Average tokens/post (before):  {avg_before:.0f}")
    print(f"Average tokens/post (after):   {avg_after:.0f}")
    print(f"Average savings/post:          {avg_savings:.0f} tokens ({avg_savings_pct:.1f}%)")
    print()

    # Session budget calculation
    session_tokens = 30000  # 5-hour session
    posts_before = session_tokens / avg_before
    posts_after = session_tokens / avg_after
    improvement = posts_after / posts_before

    print(f"💰 With a {session_tokens:,} token budget (5-hour session):")
    print(f"   Before optimization: ~{posts_before:.0f} posts")
    print(f"   After optimization:  ~{posts_after:.0f} posts")
    print(f"   Improvement:         {improvement:.1f}x more posts from same budget")
    print()

    # Monthly estimate (assuming 100 posts/month)
    monthly_posts = 100
    tokens_before_monthly = monthly_posts * avg_before
    tokens_after_monthly = monthly_posts * avg_after
    monthly_savings = tokens_before_monthly - tokens_after_monthly
    monthly_cost_before = tokens_before_monthly * 0.00001
    monthly_cost_after = tokens_after_monthly * 0.00001
    monthly_cost_savings = monthly_cost_before - monthly_cost_after

    print(f"📈 Extrapolated to {monthly_posts} posts/month:")
    print(f"   Before: {tokens_before_monthly:,} tokens ≈ ${monthly_cost_before:.2f}/month")
    print(f"   After:  {tokens_after_monthly:,} tokens ≈ ${monthly_cost_after:.2f}/month")
    print(f"   Saves:  {monthly_savings:,} tokens ≈ ${monthly_cost_savings:.2f}/month")
    print()

    print("=" * 80)
    print("KEY OPTIMIZATIONS")
    print("=" * 80)
    print("1. OCR instead of Vision for images:")
    print(f"   - Image tokens saved: ~{sum(s['before']['image_vision'] for s in scenarios) / len(scenarios):.0f}/post")
    print()
    print("2. Single batched Claude call instead of multiple calls:")
    print(f"   - Overhead reduced: {(sum(s['before']['overhead'] for s in scenarios) / len(scenarios)):.0f} → "
          f"{(sum(s['after']['overhead'] for s in scenarios) / len(scenarios)):.0f} tokens/post")
    print()
    print("3. Structured JSON output (no markdown parsing overhead):")
    print(f"   - Parsing tokens eliminated")
    print()
    print("=" * 80)
    print()
    print("Current command: .venv/bin/python prompt-listener.py --process <url>")
    print()


if __name__ == "__main__":
    calculate_costs()
