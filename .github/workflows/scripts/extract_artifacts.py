#!/usr/bin/env python3
import os
import zipfile
import glob
import argparse
import shutil


def is_release_file(filename: str) -> bool:
    """Determine whether a file is a release file"""
    if not filename.startswith('android'):
        return False
    # android...-boot-gz.img, android...-boot-lz4.img, android...-boot.img, android...-AnyKernel3.zip
    if filename.endswith('.zip') and 'AnyKernel3' in filename:
        return True
    if filename.endswith('.img') and 'boot' in filename:
        return True
    return False


def process_artifacts(artifacts_dir: str, output_dir: str, build_results_dir: str = None):
    """Extract release files from artifact zips"""
    os.makedirs(output_dir, exist_ok=True)
    
    for name in os.listdir(artifacts_dir):
        path = os.path.join(artifacts_dir, name)
        
        if os.path.isdir(path):
            continue
        
        # Directly copy files that already qualify as release files
        if is_release_file(name):
            target = os.path.join(output_dir, name)
            shutil.copy2(path, target)
            print(f"Copied: {name}")
            continue
        
        # Only zip files need to be inspected further
        if not name.endswith('.zip'):
            continue
        
        print(f"Extracting: {name}")
        try:
            with zipfile.ZipFile(path, 'r') as zf:
                for member in zf.namelist():
                    if member.endswith('/') or member.startswith('__MACOSX/'):
                        continue
                        
                    filename = os.path.basename(member)
                    
                    if is_release_file(filename):
                        target = os.path.join(output_dir, filename)
                        with zf.open(member) as src, open(target, 'wb') as dst:
                            dst.write(src.read())
                        print(f"  Extracted: {filename}")
                    else:
                        print(f"  Skipped: {filename}")
        except zipfile.BadZipFile:
            print(f"Error: invalid zip file - {name}")
        except Exception as e:
            print(f"Error: {e}")
    
    # Merge SHA256SUMS
    if build_results_dir:
        sha256sums = []
        for txt_file in glob.glob(os.path.join(build_results_dir, '*.txt')):
            basename = os.path.basename(txt_file)
            if basename == 'status.txt' or basename.startswith('status-'):
                continue
            try:
                with open(txt_file, 'r') as f:
                    content = f.read().strip()
                    if content:
                        sha256sums.append(content)
            except Exception:
                pass
        
        if sha256sums:
            sha256_path = os.path.join(output_dir, 'SHA256SUMS.txt')
            with open(sha256_path, 'w') as f:
                f.write('\n'.join(sha256sums) + '\n')
            print(f"Generated: SHA256SUMS.txt")
    
    print("Done")


def main():
    parser = argparse.ArgumentParser(description="Extract release files")
    parser.add_argument("artifacts_dir", help="Path to the artifacts directory")
    parser.add_argument("output_dir", help="Path to the output directory")
    parser.add_argument("--build-results", help="Path to the build-results directory", default=None)
    args = parser.parse_args()
    
    process_artifacts(args.artifacts_dir, args.output_dir, args.build_results)


if __name__ == "__main__":
    main()
