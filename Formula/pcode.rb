class Pcode < Formula
  desc "Terminal-native AI coding agent built for long-running and parallel work"
  homepage "https://github.com/cruxwell/pcode"
  url "https://github.com/cruxwell/pcode/releases/download/v0.1.7/pcode-0.1.7-src.tar.gz"
  sha256 "50dd607dbe67118e964c7e03a6005cac89412ca56c8eab0d4fdd49cf1b9cff47"
  head "https://github.com/cruxwell/pcode.git", branch: "master"

  # Prebuilt extension modules (jiter's, for one) carry @rpath dylib IDs with no
  # header room for the Cellar path Homebrew would write, which fails the
  # install. Nothing links against them, so their IDs can stay as built.
  preserve_rpath

  depends_on "uv" => :build
  depends_on "git-delta"
  depends_on "python@3.15"
  depends_on "shfmt"

  # The sandbox extension's shell sandbox on Linux (macOS ships sandbox-exec).
  on_linux do
    depends_on "bubblewrap"
  end

  # This upstream tap uses uv.lock rather than duplicating its dependency tree
  # as Homebrew resources. Dependency downloads require network access at build time.
  def install
    libexec.install "pyproject.toml", "uv.lock", "src", "README.md", "LICENSE"
    # The package version comes from git tags, and no .git is staged here, so
    # name it: the release itself, or 0.dev0+g<commit> for a HEAD build.
    # Scoped to pcode, so a dependency built from source keeps its own.
    ENV["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PCODE"] = version.head? ? "0.dev0+g#{version.commit}" : version.to_s
    ENV["UV_PYTHON_DOWNLOADS"] = "never"
    ENV["UV_PROJECT_ENVIRONMENT"] = libexec/".venv"
    ENV["UV_LINK_MODE"] = "copy"
    system "uv", "sync", "--directory", libexec, "--locked", "--no-dev", "--extra", "claude",
                 "--no-editable", "--no-cache", "--python", formula_opt_bin("python@3.15")/"python3.15"
    bin.install_symlink libexec/".venv/bin/pcode"
    generate_completions_from_executable(bin/"pcode", "--completions")
  end

  # Homebrew's post-install relocation rewrites the universal2 (x86_64+arm64)
  # extension modules from PyPI wheels in place. The bytes come out identical,
  # but macOS then kills any Python that loads them (CODESIGNING "Invalid
  # Page"). Re-signing writes fresh files, which clears that state.
  post_install_steps do
    on_macos do
      run "/usr/bin/find", args: [
        ".", "(", "-name", "*.so", "-o", "-name", "*.dylib", ")",
        "-exec", "/usr/bin/codesign", "--force", "--sign", "-", "{}", "+"
      ], chdir: "{{libexec}}/.venv/lib"
    end
  end

  test do
    assert_match "--model", shell_output("#{bin}/pcode --help")
    assert_match "No saved sessions.",
                 shell_output("#{bin}/pcode --sessions --session-dir #{testpath}/sessions")
    assert_match "pcode", shell_output("#{bin}/pcode --theme-preview")
  end
end
