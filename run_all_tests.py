import subprocess
import sys

def main():
    print("Running all tests with coverage...\n")
    result = subprocess.run(
        ["pytest", "--maxfail=1", "--disable-warnings", "--cov=src", "--cov-report=term-missing", "-v"],
        stdout=sys.stdout,
        stderr=sys.stderr
    )
    if result.returncode != 0:
        print("\n❌ Some tests failed. Fix issues before running the bot.")
        sys.exit(result.returncode)
    else:
        print("\n✅ All tests passed successfully.")

if __name__ == "__main__":
    main()