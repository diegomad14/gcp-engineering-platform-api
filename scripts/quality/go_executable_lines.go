// Trusted syntax helper: statement starts, never comments or declarations.
package main

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"sort"
)

func main() {
	result := map[string][][2]int{}
	for _, path := range os.Args[1:] {
		if path == "--" {
			continue
		}
		fs := token.NewFileSet()
		file, err := parser.ParseFile(fs, path, nil, 0)
		if err != nil {
			panic(err)
		}
		lines := map[[2]int]bool{}
		ast.Inspect(file, func(n ast.Node) bool {
			switch n.(type) {
			case *ast.BlockStmt, *ast.EmptyStmt, *ast.CaseClause, *ast.CommClause, *ast.LabeledStmt:
			default:
				if _, ok := n.(ast.Stmt); ok {
					position := fs.Position(n.Pos())
					lines[[2]int{position.Line, position.Column}] = true
				}
			}
			return true
		})
		result[path] = [][2]int{}
		for line := range lines {
			result[path] = append(result[path], line)
		}
		sort.Slice(result[path], func(i, j int) bool {
			a, b := result[path][i], result[path][j]
			return a[0] < b[0] || (a[0] == b[0] && a[1] < b[1])
		})
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		panic(err)
	}
}
