class Trustmux < Formula
  include Language::Python::Virtualenv

  desc "Monitor and interact with tmux/Byobu sessions from your phone"
  homepage "https://trustmux.app"

  url "https://files.pythonhosted.org/packages/fd/d7/2950498369e93cbd301fe8d528efe3a5c248d263164c1ac69fc9d950a11e/trustmux-7.0.tar.gz"
  sha256 "61bb6de895226595d9a99936f41965b3421e55b826474f572b5aefabbab969e9"
  version "7.0"
  license "GPL-3.0-or-later"

  head "https://github.com/dustinkirkland/byobu.git", branch: "master"

  depends_on "python@3.12"
  depends_on "tmux"

  resource "tornado" do
    url "https://files.pythonhosted.org/packages/50/57/6d7303a77ae439d9189108f76c0c4fd89ee5e2cc8387bffb55232565c4ed/tornado-6.5.6.tar.gz"
    sha256 "9a365179fe8ff6b8766f602c0f67c185d778193e9bdd828b19f0b6ed7764177d"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    # Daemon is not running — status exits 0 and prints "not running"
    assert_match "trustmux not running", shell_output("#{bin}/trustmux-ctl status")
    # Pair command should accept --help without needing a running daemon
    assert_match "usage:", shell_output("#{bin}/trustmux-ctl --help")
  end
end
