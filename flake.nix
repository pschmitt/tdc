{
  description = "Todoist CLI written in Python";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        tdc = pkgs.python3Packages.buildPythonApplication {
          pname = "tdc";
          version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
          pyproject = true;

          src = ./.;

          buildInputs = [
            pkgs.python3Packages.setuptools
            pkgs.python3Packages.wheel
          ];

          propagatedBuildInputs = with pkgs.python3Packages; [
            regex
            rich-argparse
            todoist-api-python
            wcwidth
          ];

          meta = {
            description = "Todoist CLI, powered by rich";
            homepage = "https://github.com/pschmitt/tdc";
            license = pkgs.lib.licenses.gpl3Only;
            maintainers = with pkgs.lib.maintainers; [ pschmitt ];
            platforms = pkgs.lib.platforms.all;
            mainProgram = "tdc";
          };
        };

        devShell = pkgs.mkShell {
          name = "tdc-devshell";

          inputsFrom = [ tdc ];

          # Additional development tools
          nativeBuildInputs = [
            pkgs.gh # GitHub CLI
            pkgs.git
            pkgs.python3Packages.ipython
            pkgs.neovim
          ];

          # Environment variables and shell hooks
          shellHook = ''
            export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
            echo -e "\e[34mWelcome to the tdc development shell!\e[0m"
            # Activate a virtual environment if desired
            # source .venv/bin/activate
          '';

          # Optional: Set up a Python virtual environment
          # if you prefer using virtualenv or similar tools
          # you can uncomment and configure the following lines
          # shellHook = ''
          #   if [ ! -d .venv ]; then
          #     python3 -m venv .venv
          #     source .venv/bin/activate
          #     pip install --upgrade pip
          #   else
          #     source .venv/bin/activate
          #   fi
          # '';
        };
      in
      {
        # pkgs
        packages.tdc = tdc;
        packages.default = tdc;

        devShells.default = devShell;
      }
    );
}
