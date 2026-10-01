#!/bin/bash
llama serve -hf unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL   --port 8081 -c 65535 -np 1 --reasoning off   --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0 --presence-penalty 0   -t 14 -tb 16 -fa on --image-max-tokens 1024
