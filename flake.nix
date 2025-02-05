{
  description = "Todoist CLI written in Python";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      ...
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        tdc = pkgs.python3Packages.buildPythonApplication {
          pname = "tdc";
          version = "latest";
          pyproject = true;

          src = ./.;

          buildInputs = [
            pkgs.python3Packages.setuptools
            pkgs.python3Packages.wheel
          ];

          propagatedBuildInputs = with pkgs.python3Packages; [
            rich-argparse
            todoist-api-python
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

          buildInputs = [
            pkgs.python3
            pkgs.python3Packages.setuptools
            pkgs.python3Packages.wheel
            pkgs.python3Packages.rich
            pkgs.python3Packages.rich-argparse
            pkgs.python3Packages.todoist-api-python
          ];

          # Additional development tools
          nativeBuildInputs = [
            pkgs.gh # GitHub CLI
            pkgs.git
            pkgs.python3Packages.ipython
            pkgs.neovim
          ];

          # Environment variables and shell hooks
          shellHook = ''
            export PYTHONPATH=${self.packages.${system}.tdc}/lib/python3.x/site-packages
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
        defaultPackage = tdc;

        devShells.default = devShell;
      }
    );
}
