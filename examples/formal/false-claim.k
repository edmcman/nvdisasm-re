requires "sass.md"

// This claim must fail. It is deliberately separate from the passing claims.
module SASS-FALSE-CLAIM
  imports SASS
  claim <k> eval(node(0, "add", 32,
                       ListItem(node(1, "const", 32, ListItem(1)))
                       ListItem(node(2, "const", 32, ListItem(1))))) => 3 ... </k>
        <memo> .Map => ?CACHE:Map </memo>
endmodule
