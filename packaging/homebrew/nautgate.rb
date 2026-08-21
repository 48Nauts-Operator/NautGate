#  NautGate — Homebrew formula (native install, alongside the Docker stack)
#
#  Docker remains the recommended path and is unchanged. This is for people who
#  would rather not run Docker for a gateway, and for `brew services` to
#  supervise it like any other daemon.
#
#  Deliberate choices, and why:
#
#  * The app is installed into libexec as a plain source tree with its own
#    virtualenv, and `bin/nautgate` is a wrapper. NautGate is not a Python
#    package — pyproject sets `[tool.uv] package = false` and the Docker image
#    installs with `--no-install-project` — so packaging it just to satisfy
#    Homebrew would change how every contributor's sync and the image build
#    behave, for one wrapper this formula can write itself.
#
#  * Postgres is a dependency, not a bundled service. The gateway stores an
#    audit log; owning someone's database lifecycle is not this formula's job.
#    `caveats` tells the operator how to create the database once.
#
#  * NautRouter (the routing sidecar) is Node and ships separately. The gateway
#    runs without it — routed models are unavailable until NAUTROUTER_BASE_URL
#    points somewhere — so it is not a hard dependency here.
#
#  * The service binds 127.0.0.1. A native install has no container boundary,
#    and binding every interface is how the dashboard ended up readable from a
#    whole tailnet. Exposing it is an explicit choice, not a default.
#
class Nautgate < Formula
  include Language::Python::Virtualenv

  desc "Memory-aware LLM gateway — routes, records and prices every model call"
  homepage "https://nautgate.dev"
  url "https://github.com/48Nauts-Operator/NautGate/archive/refs/tags/v0.4.0.tar.gz"
  sha256 "38a61ccfa92fb7af74dd3a08aaa8139608681b22c736784a99acb5a0d6c1c7ca"
  license "AGPL-3.0-or-later"
  head "https://github.com/48Nauts-Operator/NautGate.git", branch: "main"

  # libyaml: pyyaml builds its C extension against it — brew audit catches
  # this, the build would fail without it.
  depends_on "libyaml"
  depends_on "postgresql@16"
  depends_on "python@3.12"

  # Resources are GENERATED, never hand-edited:
  #   uv run --project core python packaging/homebrew/gen_resources.py --write
  #   uv run --project core python packaging/homebrew/gen_resources.py --check
  # brew update-python-resources cannot help here — it resolves the formula's
  # own package from PyPI and NautGate is not published there, so uv.lock is the
  # source of truth. A stale list installs old dependencies silently.
  # mitmproxy is deliberately absent: an optional extra, and a third of the
  # install.

  # BEGIN GENERATED PYTHON RESOURCES
  resource "annotated-doc" do
    url "https://files.pythonhosted.org/packages/57/ba/046ceea27344560984e26a590f90bc7f4a75b06701f653222458922b558c/annotated_doc-0.0.4.tar.gz"
    sha256 "fbcda96e87e9c92ad167c2e53839e57503ecfda18804ea28102353485033faa4"
  end

  resource "annotated-types" do
    url "https://files.pythonhosted.org/packages/ee/67/531ea369ba64dcff5ec9c3402f9f51bf748cec26dde048a2f973a4eea7f5/annotated_types-0.7.0.tar.gz"
    sha256 "aff07c09a53a08bc8cfccb9c85b05f1aa9a2a6f23728d790723543408344ce89"
  end

  resource "anyio" do
    url "https://files.pythonhosted.org/packages/19/14/2c5dd9f512b66549ae92767a9c7b330ae88e1932ca57876909410251fe13/anyio-4.13.0.tar.gz"
    sha256 "334b70e641fd2221c1505b3890c69882fe4a2df910cba14d97019b90b24439dc"
  end

  resource "argon2-cffi" do
    url "https://files.pythonhosted.org/packages/0e/89/ce5af8a7d472a67cc819d5d998aa8c82c5d860608c4db9f46f1162d7dab9/argon2_cffi-25.1.0.tar.gz"
    sha256 "694ae5cc8a42f4c4e2bf2ca0e64e51e23a040c6a517a85074683d3959e1346c1"
  end

  resource "argon2-cffi-bindings" do
    url "https://files.pythonhosted.org/packages/5c/2d/db8af0df73c1cf454f71b2bbe5e356b8c1f8041c979f505b3d3186e520a9/argon2_cffi_bindings-25.1.0.tar.gz"
    sha256 "b957f3e6ea4d55d820e40ff76f450952807013d361a65d7f28acc0acbf29229d"
  end

  resource "asyncpg" do
    url "https://files.pythonhosted.org/packages/fe/cc/d18065ce2380d80b1bcce927c24a2642efd38918e33fd724bc4bca904877/asyncpg-0.31.0.tar.gz"
    sha256 "c989386c83940bfbd787180f2b1519415e2d3d6277a70d9d0f0145ac73500735"
  end

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/25/ee/6caf7a40c36a1220410afe15a1cc64993a1f864871f698c0f93acb72842a/certifi-2026.4.22.tar.gz"
    sha256 "8d455352a37b71bf76a79caa83a3d6c25afee4a385d632127b6afb3963f1c580"
  end

  resource "cffi" do
    url "https://files.pythonhosted.org/packages/eb/56/b1ba7935a17738ae8453301356628e8147c79dbb825bcbc73dc7401f9846/cffi-2.0.0.tar.gz"
    sha256 "44d1b5909021139fe36001ae048dbdde8214afa20200eda0f64c068cac5d5529"
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/bb/63/f9e1ea081ce35720d8b92acde70daaedace594dc93b693c869e0d5910718/click-8.3.3.tar.gz"
    sha256 "398329ad4837b2ff7cbe1dd166a4c0f8900c3ca3a218de04466f38f6497f18a2"
  end

  resource "cryptography" do
    url "https://files.pythonhosted.org/packages/12/45/870e7f4bef50e5f53b9f51d4428aee5290eedf58ba443f16b1ebb7ab8e66/cryptography-48.0.1.tar.gz"
    sha256 "266f4ee051abb2f725b74ef8072b521ce1feacf685a3364fa6a6b45548db791a"
  end

  resource "fastapi" do
    url "https://files.pythonhosted.org/packages/5d/45/c130091c2dfa061bbfe3150f2a5091ef1adf149f2a8d2ae769ecaf6e99a2/fastapi-0.136.1.tar.gz"
    sha256 "7af665ad7acfa0a3baf8983d393b6b471b9da10ede59c60045f49fbc89a0fa7f"
  end

  resource "h11" do
    url "https://files.pythonhosted.org/packages/01/ee/02a2c011bdab74c6fb3c75474d40b3052059d95df7e73351460c8588d963/h11-0.16.0.tar.gz"
    sha256 "4e35b956cf45792e4caa5885e69fba00bdbc6ffafbfa020300e549b208ee5ff1"
  end

  resource "httpcore" do
    url "https://files.pythonhosted.org/packages/06/94/82699a10bca87a5556c9c59b5963f2d039dbd239f25bc2a63907a05a14cb/httpcore-1.0.9.tar.gz"
    sha256 "6e34463af53fd2ab5d807f399a9b45ea31c3dfa2276f15a2c3f00afff6e176e8"
  end

  resource "httptools" do
    url "https://files.pythonhosted.org/packages/b5/46/120a669232c7bdedb9d52d4aeae7e6c7dfe151e99dc70802e2fc7a5e1993/httptools-0.7.1.tar.gz"
    sha256 "abd72556974f8e7c74a259655924a717a2365b236c882c3f6f8a45fe94703ac9"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/b1/df/48c586a5fe32a0f01324ee087459e112ebb7224f646c0b5023f5e79e9956/httpx-0.28.1.tar.gz"
    sha256 "75e98c5f16b0f35b567856f597f06ff2270a374470a5c2392242528e3e3e42fc"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/ce/cc/762dfb036166873f0059f3b7de4565e1b5bc3d6f28a414c13da27e442f99/idna-3.13.tar.gz"
    sha256 "585ea8fe5d69b9181ec1afba340451fba6ba764af97026f92a91d4eef164a242"
  end

  resource "pycparser" do
    url "https://files.pythonhosted.org/packages/1b/7d/92392ff7815c21062bea51aa7b87d45576f649f16458d78b7cf94b9ab2e6/pycparser-3.0.tar.gz"
    sha256 "600f49d217304a5902ac3c37e1281c9fe94e4d0489de643a9504c5cdfdfc6b29"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/ae/54/ecab642b3bed45f7d5f59b38443dcb36ef50f85af192e6ece103dbfe9587/pydantic-2.11.10.tar.gz"
    sha256 "dc280f0982fbda6c38fada4e476dc0a4f3aeaf9c6ad4c28df68a666ec3c61423"
  end

  resource "pydantic-core" do
    url "https://files.pythonhosted.org/packages/ad/88/5f2260bdfae97aabf98f1778d43f69574390ad787afb646292a638c923d4/pydantic_core-2.33.2.tar.gz"
    sha256 "7cb8bc3605c29176e1b105350d2e6474142d7c1bd1d9327c4a9bdb46bf827acc"
  end

  resource "pydantic-settings" do
    url "https://files.pythonhosted.org/packages/42/98/c8345dccdc31de4228c039a98f6467a941e39558da41c1744fbe29fa5666/pydantic_settings-2.14.0.tar.gz"
    sha256 "24285fd4b0e0c06507dd9fdfd331ee23794305352aaec8fc4eb92d4047aeb67d"
  end

  resource "python-dotenv" do
    url "https://files.pythonhosted.org/packages/82/ed/0301aeeac3e5353ef3d94b6ec08bbcabd04a72018415dcb29e588514bba8/python_dotenv-1.2.2.tar.gz"
    sha256 "2c371a91fbd7ba082c2c1dc1f8bf89ca22564a087c2c287cd9b662adde799cf3"
  end

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz"
    sha256 "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
  end

  resource "starlette" do
    url "https://files.pythonhosted.org/packages/81/69/17425771797c36cded50b7fe44e850315d039f28b15901ab44839e70b593/starlette-1.0.0.tar.gz"
    sha256 "6a4beaf1f81bb472fd19ea9b918b50dc3a77a6f2e190a12954b25e6ed5eea149"
  end

  resource "structlog" do
    url "https://files.pythonhosted.org/packages/ef/52/9ba0f43b686e7f3ddfeaa78ac3af750292662284b3661e91ad5494f21dbc/structlog-25.5.0.tar.gz"
    sha256 "098522a3bebed9153d4570c6d0288abf80a031dfdb2048d59a49e9dc2190fc98"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/d1/bc/51647cd02527e87d05cb083ccc402f93e441606ff1f01739a62c8ad09ba5/typing_extensions-4.14.0.tar.gz"
    sha256 "8676b788e32f02ab42d9e7c61324048ae4c6d844a399eebace3d4979d75ceef4"
  end

  resource "typing-inspection" do
    url "https://files.pythonhosted.org/packages/55/e3/70399cb7dd41c10ac53367ae42139cf4b1ca5f36bb3dc6c9d33acdb43655/typing_inspection-0.4.2.tar.gz"
    sha256 "ba561c48a67c5958007083d386c3295464928b01faa735ab8547c5692e87f464"
  end

  resource "uvicorn" do
    url "https://files.pythonhosted.org/packages/1f/93/041fca8274050e40e6791f267d82e0e2e27dd165627bd640d3e0e378d877/uvicorn-0.46.0.tar.gz"
    sha256 "fb9da0926999cc6cb22dc7cd71a94a632f078e6ae47ff683c5c420750fb7413d"
  end

  resource "uvloop" do
    url "https://files.pythonhosted.org/packages/06/f0/18d39dbd1971d6d62c4629cc7fa67f74821b0dc1f5a77af43719de7936a7/uvloop-0.22.1.tar.gz"
    sha256 "6c84bae345b9147082b17371e3dd5d42775bddce91f885499017f4607fdaf39f"
  end

  resource "watchfiles" do
    url "https://files.pythonhosted.org/packages/c2/c9/8869df9b2a2d6c59d79220a4db37679e74f807c559ffe5265e08b227a210/watchfiles-1.1.1.tar.gz"
    sha256 "a173cb5c16c4f40ab19cecf48a534c409f7ea983ab8fed0741304a1c0a31b3f2"
  end

  resource "websockets" do
    url "https://files.pythonhosted.org/packages/04/24/4b2031d72e840ce4c1ccb255f693b15c334757fc50023e4db9537080b8c4/websockets-16.0.tar.gz"
    sha256 "5f6261a5e56e8d5c42a4497b364ea24d94d9563e8fbd44e78ac40879c60179b5"
  end

  # END GENERATED PYTHON RESOURCES

  def install
    # Ship the source tree, not an installed package (see the header).
    libexec.install Dir["core/*"]
    (libexec/"config").install Dir["config/*"]

    venv = virtualenv_create(libexec/"venv", "python3.12")
    venv.pip_install resources

    (bin/"nautgate").write <<~SH
      #!/bin/bash
      # PYTHONPATH rather than an installed package — NautGate is run as a
      # source tree in every deployment shape it supports.
      export PYTHONPATH="#{libexec}"
      export NAUTGATE_CONFIG_DIR="${NAUTGATE_CONFIG_DIR:-#{libexec}/config}"
      # A brew service does not inherit the shell that launched it. Give the
      # standard local database a durable default while preserving an explicit
      # override for foreground/native runs.
      export NAUTGATE_DB_URL="${NAUTGATE_DB_URL:-postgres://$(id -un)@localhost:5432/nautgate}"
      exec "#{libexec}/venv/bin/python" -m app.cli "$@"
    SH
    chmod 0755, bin/"nautgate"
  end

  service do
    run [opt_bin/"nautgate", "serve"]
    keep_alive true
    working_dir var
    log_path var/"log/nautgate.log"
    error_log_path var/"log/nautgate.log"
    # No environment_variables block for credentials on purpose: provider keys
    # belong in the dashboard's encrypted store or the operator's own env, not
    # in a plist that ends up world-readable in /opt/homebrew.
  end

  def caveats
    <<~EOS
      NautGate needs a Postgres database. Once:

        brew services start postgresql@16
        "$(brew --prefix postgresql@16)/bin/createdb" nautgate

      Then start the gateway:

        brew services start nautgate

      The wrapper defaults NAUTGATE_DB_URL to the database above, including when
      launchd starts it. Set NAUTGATE_DB_URL explicitly when running
      `nautgate serve` in the foreground to use a different database.

      The dashboard is on http://127.0.0.1:8090/dashboard and the first-run API
      key is printed to #{var}/log/nautgate.log the first time it starts with an
      empty database. It is printed once — copy it then.

      It binds 127.0.0.1 by default. Exposing it to your network means anyone who
      can reach the port can read the audit log, so that is opt-in:

        NAUTGATE_HOST=0.0.0.0 nautgate serve

      Routed models (model: auto and the tiers) need NautRouter running:

        export NAUTROUTER_BASE_URL="http://localhost:8404"

      Without it the passthrough lanes still work.
    EOS
  end

  test do
    assert_match "nautgate", shell_output("#{bin}/nautgate --version")
    # `status` must exit non-zero and name the address when nothing is listening,
    # rather than reporting a bare "not running".
    out = shell_output("#{bin}/nautgate status --port 9", 1)
    assert_match "127.0.0.1:9", out
  end
end
