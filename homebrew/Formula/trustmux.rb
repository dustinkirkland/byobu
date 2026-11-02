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
  depends_on "rust" => :build  # required to compile cryptography's Rust extension

  resource "tornado" do
    url "https://files.pythonhosted.org/packages/d0/02/5f549b1ad5da6267ed1e1fac2eef69257b1979cc812497002499b7895ff9/tornado-6.6.tar.gz"
    sha256 "34c4aa916d7f2b2d5cadee2c6850f5a4a7f3d688cbf58399ea9bd3057af4abd5"
  end

  resource "pycparser" do
    url "https://files.pythonhosted.org/packages/cb/27/eeb9f307bf07060f3d9f9aa0fb8c15b01bdfded0a941e4b2719c7f94cacd/pycparser-3.12.tar.gz"
    sha256 "f03387877eab82ce01bbdfd0ba7252ee55a90b1c708d5129e2ce915475cf1db6"
  end

  resource "cffi" do
    url "https://files.pythonhosted.org/packages/9e/ef/008a1939e372c06329a3fce4279c02f328488f3526744906eeec3da7ad5f/cffi-2.1.1.tar.gz"
    sha256 "dd31f52ea1086513bb9df30f8fcee9b8918323ae067a3d5b78bc826a000712be"
  end

  resource "cryptography" do
    url "https://files.pythonhosted.org/packages/9d/af/182eb91b0df3fe75c4d9f26fe70684569566745f6ba7e5c9c73a862c5252/cryptography-50.0.2.tar.gz"
    sha256 "7b46165bb56eb4704e2eaaf86f3c940d19154535d9b0ca7d6d590b04060e00d5"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    # Daemon is not running — status exits 0 and prints "not running"
    assert_match "trustmux not running", shell_output("#{bin}/trustmux status")
    # --help should work without a running daemon
    assert_match "usage:", shell_output("#{bin}/trustmux --help")
    # Regression: cryptography must be importable in the bundled venv (GH: #113)
    system libexec/"bin/python3", "-c",
      "from cryptography.hazmat.primitives.asymmetric import ec; ec.generate_private_key(ec.SECP256R1())"
  end
end
