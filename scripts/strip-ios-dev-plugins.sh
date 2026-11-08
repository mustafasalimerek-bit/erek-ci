#!/usr/bin/env bash
# usage: strip-ios-dev-plugins.sh <app_dir>
# Only the disposable iOS build checkout should call this, never the test job.
set -euo pipefail

main() {
  if [ "$#" -ne 1 ]; then
    echo "usage: $(basename "$0") <app_dir>" >&2
    exit 2
  fi
  local removed_file
  removed_file=$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ios-dev-plugins-removed.XXXXXX")
  cd "$1"

  flutter pub get
  # Psych is part of Ruby (already required by CocoaPods). Use source locations
  # instead of dumping YAML, which would rewrite unrelated pubspec entries.
  ruby - "$removed_file" <<'RUBY'
require 'json'
require 'psych'

begin
  plugins = JSON.parse(File.read('.flutter-plugins-dependencies')).fetch('plugins').fetch('ios', [])
  names = plugins.select { |plugin| plugin['dev_dependency'] == true }.map { |plugin| plugin.fetch('name') }.uniq.sort
  raise 'invalid iOS plugin name' unless names.all? { |name| /\A[a-z_][a-z0-9_]*\z/.match?(name) }
  removed = []
  unless names.empty?
    source = File.binread('pubspec.yaml')
    document = Psych.parse(source)
    root = document.root
    raise 'pubspec must be a YAML mapping' unless root.is_a?(Psych::Nodes::Mapping)
    section = root.children.each_slice(2).find { |key, _| key.is_a?(Psych::Nodes::Scalar) && key.value == 'dev_dependencies' }
    if section
      dev = section.last
      unless dev.is_a?(Psych::Nodes::Mapping) && !dev.style.eql?(Psych::Nodes::Mapping::FLOW)
        raise 'dev_dependencies must be a block mapping to remove entries safely'
      end
      # Container end positions can include comments belonging to the next
      # entry. Only remove through the last actual value in this entry.
      last_line = lambda do |node|
        children = node.children || []
        own_end = node.end_line - (node.end_column.zero? ? 1 : 0)
        flow = (node.is_a?(Psych::Nodes::Mapping) && node.style == Psych::Nodes::Mapping::FLOW) ||
               (node.is_a?(Psych::Nodes::Sequence) && node.style == Psych::Nodes::Sequence::FLOW)
        children.empty? || flow ? own_end : children.map { |child| last_line.call(child) }.max
      end
      deleted_lines = []
      dev.children.each_slice(2) do |key, value|
        next unless key.is_a?(Psych::Nodes::Scalar) && names.include?(key.value)
        deleted_lines.concat((key.start_line..[key.start_line, last_line.call(value)].max).to_a)
        removed << key.value
      end
      unless removed.empty?
        lines = source.lines
        File.binwrite('pubspec.yaml', lines.each_with_index.reject { |_, index| deleted_lines.include?(index) }.map(&:first).join)
      end
    end
  end
  File.write(ARGV.fetch(0), removed.sort.map { |name| "#{name}\n" }.join)
  if removed.empty?
    puts 'No direct iOS dev plugins to remove; pubspec.yaml unchanged.'
  else
    puts "Removed iOS dev plugins: #{removed.sort.join(', ')}"
  end
rescue StandardError => error
  warn "::error::Could not strip iOS dev plugins: #{error.message}"
  exit 1
end
RUBY

  flutter pub get
  ruby <<'RUBY'
require 'json'

begin
  plugins = JSON.parse(File.read('.flutter-plugins-dependencies')).fetch('plugins').fetch('ios', [])
  remaining = plugins.select { |plugin| plugin['dev_dependency'] == true }.map { |plugin| plugin.fetch('name') }.uniq.sort
  unless remaining.empty?
    warn "::error::iOS dev plugins remain after stripping direct dev_dependencies: #{remaining.join(', ')}"
    exit 1
  end
  puts 'iOS dev-plugin metadata gate passed: no dev_dependency plugins remain.'
rescue StandardError => error
  warn "::error::Could not verify iOS dev plugins: #{error.message}"
  exit 1
end
RUBY

  echo "Removed-plugin manifest: $removed_file"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf 'removed_file=%s\n' "$removed_file" >> "$GITHUB_OUTPUT"
  fi
}

main "$@"
