class Trustmux < Formula
  include Language::Python::Virtualenv

  desc "Monitor and interact with tmux/Byobu sessions from your phone"
  homepage "https://trustmux.app"

  # Stable: GitHub archive of the feature/byobu-mobile branch HEAD.
  # TODO: replace with PyPI sdist URL once published to PyPI:
  #   url "https://files.pythonhosted.org/packages/.../trustmux-7.0.0.tar.gz"
  #   sha256 "42e9963f071df472230d41a14523b5838432947639becdbf62fdb5a03362292f"
  # (also remove the `cd "mobile"` wrapper in the install block below)
  url "https://github.com/dustinkirkland/byobu/archive/cddc428a27446d4e6d4320e96928f9b4309aeb61.tar.gz"
  sha256 "2d7597a62702b5daea80b38d71b5a056333172fc6ea2f1f89fe5a2fc91515fdf"
  version "7.0.0"
  license "GPL-3.0-or-later"

  head "https://github.com/dustinkirkland/byobu.git", branch: "feature/byobu-mobile"

  depends_on "python@3.12"
  depends_on "tmux"

  resource "tornado" do
    url "https://files.pythonhosted.org/packages/50/57/6d7303a77ae439d9189108f76c0c4fd89ee5e2cc8387bffb55232565c4ed/tornado-6.5.6.tar.gz"
    sha256 "9a365179fe8ff6b8766f602c0f67c185d778193e9bdd828b19f0b6ed7764177d"
  end

  def install
    # Package root is in mobile/ subdirectory of the byobu repo.
    # Once the formula is updated to reference a PyPI sdist, remove this cd block
    # (PyPI sdists extract with pyproject.toml at the top level).
    cd "mobile" do
      virtualenv_install_with_resources
    end
  end

  test do
    # Daemon is not running — status exits 0 and prints "not running"
    assert_match "trustmux not running", shell_output("#{bin}/trustmux-ctl status")
    # Pair command should accept --help without needing a running daemon
    assert_match "usage:", shell_output("#{bin}/trustmux-ctl --help")
  end
end
