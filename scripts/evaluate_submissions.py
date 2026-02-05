#!/usr/bin/env python3
"""
Excel Submission Evaluator CLI for school-telegram-portal.
Evaluates student spreadsheet submissions against an answer key (SALES_ANALYSIS mark scheme).

Usage (run from school-telegram-portal repo root):
    python scripts/evaluate_submissions.py --answer-key ANSWER.xlsx --submission STUDENT.xlsx
    python scripts/evaluate_submissions.py --answer-key ANSWER.xlsx --batch-folder ./submissions/
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure repo root is on path when run as script
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.excel_evaluator import (
    ExcelEvaluator,
    MarkScheme,
    generate_text_report,
    generate_excel_report,
    generate_json_report,
)


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate Excel spreadsheet submissions against an answer key",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Evaluate a single submission
  python scripts/evaluate_submissions.py --answer-key SALES_ANALYSIS_ans.xlsx --submission student.xlsx

  # Batch evaluate all submissions in a folder
  python scripts/evaluate_submissions.py --answer-key SALES_ANALYSIS_ans.xlsx --batch-folder ./submissions/

  # Generate all report formats
  python scripts/evaluate_submissions.py --answer-key SALES_ANALYSIS_ans.xlsx --batch-folder ./submissions/ --output-format all
        """
    )

    parser.add_argument('--answer-key', '-a', required=True,
                        help='Path to the answer key Excel file')
    parser.add_argument('--submission', '-s',
                        help='Path to a single student submission')
    parser.add_argument('--batch-folder', '-b',
                        help='Path to folder containing multiple submissions')
    parser.add_argument('--output-format', '-o',
                        choices=['text', 'excel', 'json', 'all'],
                        default='text',
                        help='Output format (default: text)')
    parser.add_argument('--output-dir', '-d',
                        default='./evaluation_results',
                        help='Output directory for reports (default: ./evaluation_results)')

    args = parser.parse_args()

    if not os.path.exists(args.answer_key):
        print(f"Error: Answer key not found: {args.answer_key}")
        sys.exit(1)

    if not args.submission and not args.batch_folder:
        print("Error: Must specify either --submission or --batch-folder")
        sys.exit(1)

    print(f"Loading answer key: {args.answer_key}")
    evaluator = ExcelEvaluator(args.answer_key, MarkScheme())

    files_to_evaluate = []

    if args.submission:
        if os.path.exists(args.submission):
            files_to_evaluate.append(args.submission)
        else:
            print(f"Error: Submission not found: {args.submission}")
            sys.exit(1)

    if args.batch_folder:
        if os.path.isdir(args.batch_folder):
            for f in Path(args.batch_folder).glob('*.xlsx'):
                if 'ans' not in f.name.lower():
                    files_to_evaluate.append(str(f))
        else:
            print(f"Error: Batch folder not found: {args.batch_folder}")
            sys.exit(1)

    if not files_to_evaluate:
        print("No Excel files found to evaluate.")
        sys.exit(1)

    print(f"Found {len(files_to_evaluate)} file(s) to evaluate")

    os.makedirs(args.output_dir, exist_ok=True)

    results = []
    for filepath in files_to_evaluate:
        print(f"Evaluating: {Path(filepath).name}")
        try:
            result = evaluator.evaluate(filepath)
            results.append(result)

            if args.output_format in ['text', 'all']:
                report = generate_text_report(result)
                report_path = os.path.join(
                    args.output_dir,
                    f"{Path(filepath).stem}_feedback.txt"
                )
                with open(report_path, 'w') as f:
                    f.write(report)

                if args.submission:
                    print("\n" + report)

        except Exception as e:
            print(f"Error evaluating {filepath}: {e}")

    if results:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        if args.output_format in ['excel', 'all']:
            excel_path = os.path.join(args.output_dir, f"marking_summary_{timestamp}.xlsx")
            generate_excel_report(results, excel_path)
            print(f"Summary report saved to {excel_path}")

        if args.output_format in ['json', 'all']:
            json_path = os.path.join(args.output_dir, f"marking_results_{timestamp}.json")
            generate_json_report(results, json_path)
            print(f"JSON report saved to {json_path}")

        print("\n" + "=" * 50)
        print("EVALUATION COMPLETE")
        print("=" * 50)
        print(f"Files evaluated: {len(results)}")
        avg_score = sum(r.percentage for r in results) / len(results)
        print(f"Average score: {avg_score:.1f}%")
        print(f"Results saved to: {args.output_dir}")


if __name__ == "__main__":
    main()
